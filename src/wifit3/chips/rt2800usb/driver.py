"""rt2800usb driver —Panda PAU09 (RT5572) / ALFA AWUS051NH v2 (RT3572).

  * RT3572 (silicon RT3572): 2.4 GHz, 2T2R — DONE (M-A1).
                              5 GHz, 2T2R — DONE (M-A2, awaiting hw-verify).
  * RT5572 (silicon RT5592): 2.4 + 5 GHz, 2T2R — TBD (M-B1 + M-B2).

Family-shared infrastructure (transport, FW upload, MAC config, RX/TX
descriptor builders, USB end-pad / QSEL=2 / EP=0x02, EFUSE bring-up,
warm reattach) is silicon-agnostic. Per-silicon code lives in
``init_bbp_*`` / ``init_rfcsr_*`` / ``_set_channel_*`` functions,
dispatched at runtime by ``silicon_id``.

Bring-up flow (mirrors ``rt2800_probe_hw`` from
data_dumps/rt2x00-source-v6.18/rt2800lib.c, with the rt2x00 framework
+ rt2x00usb layers flattened into wifit3's per-chip module shape):

    connect()
      ├─ claim USB interface
      ├─ read_chip_id              MAC_CSR0 → silicon ID + revision      [M1]
      ├─ read_perm_mac             MAC_ADDR_DW0/DW1                      [M1]
      ├─ is_chip_warm              WLAN_EN + PBF_SYS_CTRL.READY          [M1]
      ├─ cold_bring_up
      │   ├─ rt2x00usb_load_firmware                                     [M2a]
      │   ├─ rt2800_init_registers                                       [M2b]
      │   ├─ rt2800_init_bbp                                             [M2b]
      │   ├─ rt2800_init_rfcsr_5370                                      [M2c]
      │   └─ rt2800_enable_radio
      ├─ probe_endpoints + RX loop                                       [M3]
      └─ set_channel(default)                                            [M4]

Milestone status:
  * M1:  chip-id probe + warm detection.                              [DONE]
  * M2a:  rt2870.bin firmware upload + MCU boot.                       [DONE]
  * M2b-1: rt2800usb_init_registers — USB-side bootstrap.            [DONE]
  * M2b-2: rt2800_init_registers — big MAC config.                   [DONE]
  * M2b-3: rt2800_init_bbp_53xx — baseband init.                     [DONE]
  * M2c: rt2800_init_rfcsr_5392 — RF chain init.                     [DONE]
  * M3:  RX desc decode + RX loop.                                   [DONE]
  * M4: set_channel for 2.4 GHz (1..14).                             [DONE]
  * M5 (current): inject_frame builds TXINFO + TXWI + bulk-OUT 0x06
    (MGMT EP). 1 Mbps CCK + WCID broadcast + sequence-generated.
  * M6: see top-level NEXT-STEPS.md.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError

from .constants import (
    EEPROM_NIC_CONF1_ANT_DIVERSITY_MASK,
    EEPROM_NIC_CONF1_ANT_DIVERSITY_SHIFT,
    RT_RT5592,
    USB_PID_RT3572,
    USB_PID_RT5572,
    USB_VID_RALINK,
)
from wifit3.wlan.packet import WlanFrameParser

from .bbp import init_bbp, prepare_bbp
from .chan import CHANNELS_5G_NON_DFS, is_xtal_40mhz, set_channel as _set_channel
from .eeprom import parse_eeprom, read_eeprom_efuse
from .firmware import load_firmware, load_firmware_blob
from .link_tuner import LINK_TUNE_SECONDS, LinkTuner, compute_link_vgc, set_vgc
from .mac import (
    ChipId, enable_radio, is_chip_warm, read_chip_id, read_perm_mac,
    usb_init_registers, write_mac_address,
)
from .reg_init import init_registers
from .rfcsr import RfFilterCal, init_rfcsr
from .rx import parse_rx_urb, probe_endpoints, read_rx_burst, rxwi_size_for_silicon
from ..rx_reader import RxReaderThread
from .transport import RT2800USBTransport
from .tx import inject_frame as _inject_frame, txwi_size_for_silicon

logger = logging.getLogger(__name__)


class RT2800USBDriver:
    """Driver for the rt2800usb family (RT3572 / RT5572).

    Per-variant differences (RX/TX desc size, RF init, 5 GHz support)
    are dispatched at runtime via the ``chip_id`` carried in DeviceID
    extras + the silicon ID read from MAC_CSR0 at connect() time.
    """

    SUPPORTED_IDS = [
        DeviceID(USB_VID_RALINK, USB_PID_RT3572,
                 "Ralink RT3572 (ALFA AWUS051NH v2)",
                 extras={"chip_id": "rt3572"}),
        DeviceID(USB_VID_RALINK, USB_PID_RT5572,
                 "Ralink RT5572 (Panda PAU09 N600)",
                 extras={"chip_id": "rt5572"}),
    ]
    #     0x3572 silicon (RT3572 / AWUS051NH v2) → 2.4 + 5 GHz non-DFS
    #     0x5592 silicon (RT5572 / PAU09 N600)   → 2.4 + 5 GHz non-DFS
    _CHANNELS_BY_CHIP: dict = {
        "rt3572": list(range(1, 14)) + list(CHANNELS_5G_NON_DFS),
        "rt5572": list(range(1, 14)) + list(CHANNELS_5G_NON_DFS),
    }
    # Class-level fallback = union of all variants. Used only if a hint is
    # missing (e.g. test code instantiates without going through
    # from_usb_device). Instance __init__ overlays the per-chip list.
    SUPPORTED_CHANNELS = list(range(1, 14)) + list(CHANNELS_5G_NON_DFS)
    FAKE_MAC = FakeMacSupport.SPOOFABLE

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RT2800USBDriver":
        chip_id_hint = id_entry.extras.get("chip_id", "")
        return cls(dev, chip_id_hint=chip_id_hint)

    def __init__(self, dev: usb.core.Device, *, chip_id_hint: str = ""):
        self.dev = dev
        self.transport = RT2800USBTransport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._on_lost: Optional[Callable[[Exception], None]] = None
        self._rx_reader: Optional[RxReaderThread] = None
        self._bulk_in_ep: Optional[int] = None
        # Periodic RX-AGC adaptation (rt2x00 link tuner). The accumulator is
        # fed from RX dispatch; a background task re-seeds BBP66 once a second.
        # _conf_lock serialises the tuner's register I/O against set_channel,
        # the way the kernel's conf_mutex guards the tuner vs rt2x00mac_config.
        self._link_tuner = LinkTuner()
        self._link_tuner_task: Optional[asyncio.Task] = None
        self._conf_lock = asyncio.Lock()
        self._rxwi_size: int = 16          # set at connect-time from silicon_id
        self._claimed = False
        self._eeprom = None                 # EepromValues post-EFUSE-read
        # RT3572-only: filter calibration values + saved BBP25/26 from
        # init_rfcsr_3572, replayed on every channel tune.
        self._rf_cal: Optional[RfFilterCal] = None
        # Debug instrumentation: snapshot TX-side regs on first inject + on
        # every inject (TX_STA_CNT0/1 are read-to-clear so we get TX-event
        # deltas across the burst). One-shot snapshot covers TX_PIN_CFG,
        # TX_BAND_CFG, RFCSR1/12/13 — these don't change frame-to-frame.
        self._first_inject_dumped: bool = False

        # WlanDriver Protocol surface area.
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1
        self.chip_id: Optional[ChipId] = None
        self.chip_id_hint = chip_id_hint   # from VID:PID; e.g. "rt5572"
        # Narrow the channel capability to this specific chip if we know
        # which one we're attached to. Falls through to the class-level
        # union when the hint is empty (test paths, future variants).
        per_chip = self._CHANNELS_BY_CHIP.get(chip_id_hint)
        if per_chip is not None:
            self.SUPPORTED_CHANNELS = per_chip
        # RT5592-only: probed at connect() time from MAC_DEBUG_INDEX.XTAL.
        # Picks which of rf_vals_5592_xtal20 / xtal40 the channel tune
        # consults (PAU09 N600's actual xtal isn't documented).
        self._xtal_40mhz: bool = False

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[Exception], None]) -> None:
        """Sink for a terminal RX-reader failure (unplug). Forwarded to the RxReaderThread's
        on_fatal; resolved at call time so registration order vs connect() can't strand it."""
        self._on_lost = cb

    # ---- USB claim helpers ----------------------------------------------
    def _claim(self) -> None:
        if self._claimed:
            return
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
                logger.info("detached kernel driver from interface 0")
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("kernel-driver detach skipped: %s", e)
        try:
            self.dev.set_configuration()
        except usb.core.USBError as e:
            raise IOError(f"set_configuration failed: {e}") from e
        usb.util.claim_interface(self.dev, 0)
        self._claimed = True

    def _release(self) -> None:
        if not self._claimed:
            return
        try:
            usb.util.release_interface(self.dev, 0)
            usb.util.dispose_resources(self.dev)
        except usb.core.USBError as e:
            logger.warning("USB release warning: %s", e)
        self._claimed = False

    # ---- connect --------------------------------------------------------
    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        """M1 connect: claim → identify → exit.

        M2 will wire in the real cold_bring_up.
        """
        loop = asyncio.get_event_loop()

        def _progress(pct: float, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
            logger.info("[%3d%%] %s", int(pct * 100), msg)

        try:
            _progress(0.10, "Claiming USB interface")
            await loop.run_in_executor(None, self._claim)

            _progress(0.40, "Reading MAC_CSR0 (chip ID + revision)")
            self.chip_id = await loop.run_in_executor(
                None, read_chip_id, self.transport
            )
            logger.info(
                "chip_id: %s rev=0x%04x (raw MAC_CSR0=0x%08x, hint=%s)",
                self.chip_id.name, self.chip_id.revision,
                self.chip_id.raw, self.chip_id_hint,
            )
            if not self.chip_id.is_supported:
                raise BringUpError(
                    "chip-id",
                    f"silicon ID 0x{self.chip_id.silicon_id:04x} not in the supported set "
                    f"(RT3572, RT5390, RT5592)",
                )

            _progress(0.60, "Reading permanent MAC")
            mac_bytes = await loop.run_in_executor(
                None, read_perm_mac, self.transport
            )
            self.mac_address = ":".join(f"{b:02x}" for b in mac_bytes)
            logger.info("mac_address: %s", self.mac_address)

            _progress(0.30, "Probing warm/cold state")
            warm = await loop.run_in_executor(
                None, is_chip_warm, self.transport
            )
            self.is_warm = warm
            logger.info("is_warm: %s", warm)
            if warm:
                logger.info(
                    "warm chip — re-running FW upload anyway (M2a; warm "
                    "short-circuit will land once M2b/c stabilize the "
                    "post-init register state)"
                )

            _progress(0.40, "Uploading rt2870.bin firmware + MCU boot")
            fw_bytes = await loop.run_in_executor(None, load_firmware_blob)
            try:
                await loop.run_in_executor(
                    None,
                    lambda: load_firmware(
                        self.transport,
                        fw_bytes,
                        silicon_id=self.chip_id.silicon_id,
                        progress_cb=lambda p, m: _progress(0.40 + 0.55 * p, m),
                    ),
                )
            except IOError as e:
                raise BringUpError("firmware", str(e)) from e

            _progress(0.95, "Verifying post-FW state (PBF.READY)")
            from .constants import PBF_SYS_CTRL, PBF_SYS_CTRL_READY
            pbf = await loop.run_in_executor(None, self.transport.read32, PBF_SYS_CTRL)
            if not (pbf & PBF_SYS_CTRL_READY):
                raise BringUpError("firmware", f"post-FW PBF.READY not set (PBF_SYS_CTRL=0x{pbf:08x})")
            logger.info("post-FW PBF_SYS_CTRL=0x%08x — READY latched", pbf)

            _progress(0.95, "Running rt2800usb_init_registers (M2b-1)")
            try:
                await loop.run_in_executor(None, usb_init_registers, self.transport)
            except (IOError, usb.core.USBError) as e:
                raise BringUpError("usb_init_registers", str(e)) from e

            pbf2 = await loop.run_in_executor(None, self.transport.read32, PBF_SYS_CTRL)
            pre_init = 1 << 13
            if pbf2 & pre_init:
                logger.warning(
                    "post-init PBF still has pre-init bit set (0x%08x)", pbf2
                )

            _progress(0.93, "Reading EFUSE (MAC + LNA + freq calibration)")
            try:
                eeprom_buf = await loop.run_in_executor(None, read_eeprom_efuse, self.transport)
                self._eeprom = parse_eeprom(eeprom_buf)
                self.mac_address = ":".join(f"{b:02x}" for b in self._eeprom.mac_address)
                logger.info(
                    "EFUSE: MAC=%s, lna_gain_bg=%d, freq_offset=%d, "
                    "nic_conf0=0x%04x, nic_conf1=0x%04x",
                    self.mac_address, self._eeprom.lna_gain_bg,
                    self._eeprom.freq_offset, self._eeprom.nic_conf0,
                    self._eeprom.nic_conf1,
                )
            except (IOError, usb.core.USBError) as e:
                raise BringUpError("efuse", str(e)) from e

            _progress(0.96, "Running rt2800_init_registers (M2b-2 MAC config)")
            try:
                await loop.run_in_executor(
                    None,
                    lambda: init_registers(self.transport, self.chip_id.silicon_id),
                )
            except (IOError, usb.core.USBError) as e:
                raise BringUpError("init_registers", str(e)) from e

            _progress(0.965, "Preparing BBP (MCU_BOOT_SIGNAL + wait_bbp_ready)")
            try:
                await loop.run_in_executor(None, prepare_bbp, self.transport)
            except (IOError, usb.core.USBError) as e:
                raise BringUpError("prepare_bbp", str(e)) from e

            # init_bbp consumes txpath/rxpath only to gate
            # disable_unused_dac_adc (and, on RT5592, to pick BBP antenna
            # paths); the validated chain counts are right for those.
            txpath = self._eeprom.txpath if self._eeprom else 1
            rxpath = self._eeprom.rxpath if self._eeprom else 1
            # RT3572 is the exception: its disable_unused_dac_adc powers down
            # DAC1 only when the NIC_CONF0 TXPATH field == 1 (ADC1 when RXPATH
            # == 1) — gated on the RAW EEPROM field, not a chain count.
            # [SRC] rt2800lib.c:6434-6446. On an unburned EFUSE both fields read
            # 0, so the kernel powers down neither: DAC1/ADC1 stay in their reset
            # state for the single live chain, no chain-forcing needed. Pass the
            # raw fields so we match that exactly. The TX/RX chain counts that
            # drive RFCSR1 + the PAs are separate and validated — see
            # _channel_kwargs.
            if (self.chip_id is not None
                    and self.chip_id.silicon_id == 0x3572
                    and self._eeprom is not None):
                txpath = (self._eeprom.nic_conf0 & 0x00F0) >> 4
                rxpath = self._eeprom.nic_conf0 & 0x000F
            # RT5592 needs ANT_DIVERSITY from NIC_CONF1 to pick BBP152
            # (main vs aux antenna). Kernel default-path: ant=0 (main)
            # when NIC_CONF1.ANT_DIVERSITY != 3.
            ant_diversity = 0
            if self._eeprom is not None:
                ant_diversity = (
                    (self._eeprom.nic_conf1 & EEPROM_NIC_CONF1_ANT_DIVERSITY_MASK)
                    >> EEPROM_NIC_CONF1_ANT_DIVERSITY_SHIFT
                )
            chip_rev = self.chip_id.revision

            _progress(0.97, "Running init_bbp (M2b-3 baseband init)")
            try:
                await loop.run_in_executor(
                    None,
                    lambda: init_bbp(
                        self.transport, self.chip_id.silicon_id,
                        txpath=txpath, rxpath=rxpath,
                        ant_diversity=ant_diversity,
                        chip_rev=chip_rev,
                    ),
                )
            except (IOError, usb.core.USBError, ValueError, NotImplementedError) as e:
                raise BringUpError("init_bbp", str(e)) from e

            _progress(0.98, "Running init_rfcsr (M2c RF init)")
            try:
                self._rf_cal = await loop.run_in_executor(
                    None,
                    lambda: init_rfcsr(
                        self.transport, self.chip_id.silicon_id,
                        freq_offset=self._eeprom.freq_offset if self._eeprom else 0,
                        chip_rev=chip_rev,
                    ),
                )
            except (IOError, usb.core.USBError, NotImplementedError) as e:
                raise BringUpError("init_rfcsr", str(e)) from e
            if self._rf_cal is not None:
                logger.info(
                    "RF filter cal: bw20=0x%02x bw40=0x%02x bbp25=0x%02x bbp26=0x%02x",
                    self._rf_cal.calibration_bw20, self._rf_cal.calibration_bw40,
                    self._rf_cal.bbp25, self._rf_cal.bbp26,
                )

            _progress(0.984, "Sending MCU_WAKEUP (chip → STATE_AWAKE)")
            # Kernel rt2800usb_set_device_state(STATE_RADIO_ON) does this
            # before enable_radio: send MCU_WAKEUP (0x31), sleep 1 ms,
            # then enable the radio. Without WAKEUP, the chip may stay in
            # a half-asleep state where the MAC TX FSM dequeues frames and
            # reports TX_SUCCESS in TX_STA_FIFO but the analog PA never
            # powers up — so frames go nowhere on-air even though every
            # digital indicator looks fine. RX may still work because
            # incoming-frame detection auto-wakes part of the chip.
            # [SRC] rt2800usb.c:336-351
            from .firmware import mcu_request
            try:
                await loop.run_in_executor(
                    None,
                    lambda: mcu_request(
                        self.transport, command=0x31, token=0xFF,
                        arg0=0, arg1=2,
                    ),
                )
                await asyncio.sleep(0.001)
            except (IOError, usb.core.USBError) as e:
                logger.warning("MCU_WAKEUP failed (continuing): %s", e)

            _progress(0.985, "Enabling radio (MAC TX/RX + WPDMA + USB DMA)")
            try:
                await loop.run_in_executor(
                    None,
                    lambda: enable_radio(self.transport, self.chip_id.silicon_id),
                )
            except (IOError, usb.core.USBError) as e:
                raise BringUpError("enable_radio", str(e)) from e

            # Program the EEPROM-derived MAC so RX matching engine has identity.
            await loop.run_in_executor(
                None, write_mac_address, self.transport, self._eeprom.mac_address,
            )

            # Probe xtal for RT5592 (RF5592 has dual xtal-20/40 channel
            # tables; the silicon surfaces which crystal is fitted via
            # MAC_DEBUG_INDEX.XTAL — NOT EEPROM).
            if self.chip_id.silicon_id == RT_RT5592:
                self._xtal_40mhz = await loop.run_in_executor(
                    None, is_xtal_40mhz, self.transport
                )
                logger.info(
                    "RT5592 xtal: %s MHz (MAC_DEBUG_INDEX.XTAL=%d)",
                    "40" if self._xtal_40mhz else "20", int(self._xtal_40mhz),
                )

            _progress(0.99, "Tuning to default channel 1 (M4)")
            try:
                await loop.run_in_executor(
                    None,
                    lambda: _set_channel(
                        self.transport, self.chip_id.silicon_id, 1,
                        **self._channel_kwargs(1),
                    ),
                )
                self.current_channel = 1
            except (ValueError, IOError, usb.core.USBError, NotImplementedError) as e:
                logger.warning("default-channel tune failed: %s", e)

            _progress(1.00, "Probing endpoints + starting RX loop")
            self._rxwi_size = rxwi_size_for_silicon(self.chip_id.silicon_id)
            eps = probe_endpoints(self.dev)
            self._bulk_in_ep = eps.primary_bulk_in
            self._rx_reader = RxReaderThread(
                loop, self._rx_read_once, self._rx_dispatch, name="rt2800usb-rx",
                on_fatal=lambda e: self._on_lost and self._on_lost(e))
            self._rx_reader.start()

            # Start the RX-AGC link tuner now that the channel is tuned and
            # frames are flowing (it adapts off received-frame RSSI).
            self._link_tuner.reset()
            self._link_tuner_task = loop.create_task(self._link_tuner_loop())

            self.is_warm = True
            return True

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            raise BringUpError("init", str(e)) from e

    # ---- RX loop --------------------------------------------------------
    # ---- RX callables for the shared RxReaderThread ---------------------
    # read_once runs on the reader thread; dispatch runs on the event loop.

    def _rx_read_once(self) -> Optional[bytes]:
        """One blocking bulk-IN read; None on a benign timeout."""
        return read_rx_burst(self.dev, self._bulk_in_ep)

    def _rx_dispatch(self, buf: bytes) -> None:
        """Decode one RX URB → parse → rx callback (on the loop)."""
        rx = parse_rx_urb(buf, rxwi_size=self._rxwi_size)
        if rx is None or rx.has_fcs_error:
            return
        # Feed the link tuner's RSSI average (good frames only — the kernel
        # likewise only counts successfully-received frames).
        self._link_tuner.feed(rx.rssi_dbm)
        parsed = WlanFrameParser.parse_80211_frame(rx.mpdu, rx.rssi_dbm)
        if parsed is not None and self._rx_callback is not None:
            try:
                self._rx_callback(parsed)
            except Exception as e:
                logger.exception("rx_callback raised: %s", e)

    # ---- RX-AGC link tuner ----------------------------------------------
    async def _link_tuner_loop(self) -> None:
        """Re-seed BBP66 (RX VGC) once a second from averaged RSSI.

        Ports the rt2x00 link-tuner work (see ``link_tuner.py`` for the
        algorithm + the monitor-mode deviation). A USB hiccup on one tick is
        non-fatal — we log and try again next second.
        """
        try:
            while True:
                await asyncio.sleep(LINK_TUNE_SECONDS)
                try:
                    await self._link_tuner_tick()
                except (IOError, usb.core.USBError) as e:
                    logger.debug("link tuner tick skipped: %s", e)
        except asyncio.CancelledError:
            pass

    async def _link_tuner_tick(self) -> None:
        if self._eeprom is None or self.chip_id is None:
            return
        rssi = self._link_tuner.avg_rssi()
        self._link_tuner.end_interval()
        channel = self.current_channel
        silicon = self.chip_id.silicon_id
        lna_gain = (
            self._eeprom.lna_gain_bg if channel <= 14 else self._eeprom.lna_gain_a
        )
        vgc = compute_link_vgc(silicon, channel, lna_gain, rssi)
        if vgc == self._link_tuner.vgc_level:
            return
        async with self._conf_lock:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: set_vgc(
                    self.transport, silicon, vgc,
                    rx_chain_num=self._eeprom.rxpath, rssi=rssi,
                ),
            )
        self._link_tuner.vgc_level = vgc
        logger.debug(
            "link tuner: ch=%d avg_rssi=%ddBm → BBP66 vgc=0x%02x",
            channel, rssi, vgc,
        )

    # ---- channel tune (M4) ----------------------------------------------
    def _channel_kwargs(self, channel: int = 1) -> dict:
        """Bundle the per-silicon kwargs that set_channel needs.

        RT5392 just wants freq_offset + lna_gain. RT3572 also needs
        the filter calibration + chain counts + per-band LNA gain +
        external-LNA flags from NIC_CONF1. RT5592 needs chain counts +
        BT-coex + xtal selection (but NOT cal_result — RF5592 has no
        rt2800_rx_filter_calibration step). The ``channel`` arg lets us
        pick the right per-band fields (lna_a vs lna_bg).
        """
        if self._eeprom is None:
            return {"lna_gain": 0, "freq_offset": 0}
        is_2g = channel <= 14
        lna_gain = self._eeprom.lna_gain_bg if is_2g else self._eeprom.lna_gain_a
        kwargs = {
            "lna_gain": lna_gain,
            "freq_offset": self._eeprom.freq_offset,
        }
        if self.chip_id is not None and self.chip_id.silicon_id == 0x3572:
            # TX power: RT3572's _set_channel writes RFCSR12/13.TX_POWER on
            # every tune. The kernel sources these per-channel from the EEPROM
            # TXPOWER_BG/A tables, which we don't parse yet, so we pass a
            # fallback. RFCSR12.TX_POWER is a 5-bit *backoff* code: a higher
            # value attenuates more, so the correct fallback is the LOW code the
            # in-tree driver programs, not a high one. [WIRE] aireplay.pcap on
            # this card writes RFCSR12=0x6b (chain-0 TX_POWER=11) for 2.4 GHz;
            # chain-1 (RFCSR13) TX_POWER=0 (single TX chain). 5G uses a 4-bit
            # split encoding (see _tx_power_5g) and is left at 12 pending a 5G
            # capture.
            # TODO: parse EEPROM_TXPOWER_BG1/BG2/A1/A2 per-channel tables when
            # EFUSE is burned.
            is_2g = channel <= 14
            # Chain count from the EEPROM default (the unburned fallback returns
            # txpath=1). The in-tree driver transmits on this card with a single
            # TX chain: [WIRE] aireplay.pcap configures RFCSR1/13 + TX_PIN_CFG for
            # one chain (RFCSR13 TX_POWER=0). Match it — 2T2R is not used here.
            txpath = self._eeprom.txpath
            kwargs.update(
                cal_result=self._rf_cal,
                tx_chain_num=txpath,
                rx_chain_num=self._eeprom.rxpath,
                has_cap_bt_coexist=self._eeprom.has_cap_bt_coexist,
                has_cap_external_lna_a=self._eeprom.has_cap_external_lna_a,
                default_power1=11 if is_2g else 12,
                default_power2=0 if is_2g else 12,
            )
        elif self.chip_id is not None and self.chip_id.silicon_id == RT_RT5592:
            kwargs.update(
                tx_chain_num=self._eeprom.txpath,
                rx_chain_num=self._eeprom.rxpath,
                has_cap_bt_coexist=self._eeprom.has_cap_bt_coexist,
                has_cap_external_lna_a=self._eeprom.has_cap_external_lna_a,
                xtal_40mhz=self._xtal_40mhz,
                iq_cal=self._eeprom.iq_cal,
            )
        return kwargs

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        if self.chip_id is None:
            logger.error("set_channel(%d): connect() must run first", channel)
            return False
        kwargs = self._channel_kwargs(channel)
        try:
            async with self._conf_lock:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: _set_channel(
                        self.transport, self.chip_id.silicon_id, channel,
                        **kwargs,
                    ),
                )
        except ValueError as e:
            logger.warning("rt2800usb set_channel: %s", e)
            return False
        except (IOError, usb.core.USBError, NotImplementedError) as e:
            logger.error("rt2800usb set_channel(%d): %s", channel, e)
            return False
        self.current_channel = channel
        # _set_channel just rewrote BBP66 to the per-channel AGC seed, so the
        # tuner must re-establish from scratch on the new channel (and not
        # carry the old channel's averaged RSSI across the hop).
        self._link_tuner.reset()
        return True

    # ---- TX inject (M5) -------------------------------------------------
    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        if self.chip_id is None:
            logger.error("inject_frame: connect() must run first")
            return False
        txwi_sz = txwi_size_for_silicon(self.chip_id.silicon_id)

        loop = asyncio.get_event_loop()
        if logger.isEnabledFor(logging.DEBUG) and not self._first_inject_dumped:
            await loop.run_in_executor(None, self._dump_tx_state, "pre-first-inject")
            self._first_inject_dumped = True

        logger.debug(
            "inject_frame: ch=%d len=%d no_ack=%s txwi=%dB frame=%s",
            self.current_channel, len(frame_bytes), use_no_ack, txwi_sz,
            frame_bytes[:32].hex(),
        )
        try:
            sent = await loop.run_in_executor(
                None,
                lambda: _inject_frame(
                    self.dev, frame_bytes,
                    txwi_size=txwi_sz, use_no_ack=use_no_ack,
                ),
            )
            logger.debug("inject_frame: bulk-OUT accepted %d bytes", sent)
            if logger.isEnabledFor(logging.DEBUG):
                await loop.run_in_executor(None, self._dump_tx_counters, "post-inject")
            return True
        except ValueError as e:
            logger.warning("rt2800usb inject_frame bad frame: %s", e)
            return False
        except usb.core.USBError as e:
            logger.error("rt2800usb inject_frame USBError: %s", e)
            return False

    def _dump_tx_state(self, tag: str) -> None:
        """One-shot dump of TX-side register state. Called on the first
        inject_frame so we can see what the chip thinks PA enables / band
        config / chain power-down state look like AFTER channel tune."""
        from .bbp import bbp_read
        from .constants import (
            LDO_CFG0, MAC_STATUS_CFG, MAC_SYS_CTRL, TX_BAND_CFG_REG,
            TX_PIN_CFG_REG, TX_PWR_CFG_0, TX_SW_CFG0, WPDMA_GLO_CFG,
        )
        from .rfcsr import rfcsr_read
        try:
            mac_sys = self.transport.read32(MAC_SYS_CTRL)
            tx_pin = self.transport.read32(TX_PIN_CFG_REG)
            tx_band = self.transport.read32(TX_BAND_CFG_REG)
            tx_sw0 = self.transport.read32(TX_SW_CFG0)
            mac_status = self.transport.read32(MAC_STATUS_CFG)
            wpdma = self.transport.read32(WPDMA_GLO_CFG)
            tx_pwr_cfg_0 = self.transport.read32(TX_PWR_CFG_0)
            ldo_cfg0 = self.transport.read32(LDO_CFG0)
            rfcsr1 = rfcsr_read(self.transport, 1)
            rfcsr2 = rfcsr_read(self.transport, 2)
            rfcsr3 = rfcsr_read(self.transport, 3)
            rfcsr6 = rfcsr_read(self.transport, 6)
            rfcsr12 = rfcsr_read(self.transport, 12)
            rfcsr13 = rfcsr_read(self.transport, 13)
            bbp1 = bbp_read(self.transport, 1)
            logger.debug(
                "[%s] MAC_SYS=0x%08x MAC_STATUS=0x%08x WPDMA=0x%08x "
                "TX_PIN=0x%08x TX_BAND=0x%08x TX_SW0=0x%08x "
                "TX_PWR_0=0x%08x LDO_CFG0=0x%08x BBP1=0x%02x "
                "RFCSR1=0x%02x RFCSR2=0x%02x RFCSR3=0x%02x RFCSR6=0x%02x "
                "RFCSR12=0x%02x RFCSR13=0x%02x",
                tag, mac_sys, mac_status, wpdma,
                tx_pin, tx_band, tx_sw0, tx_pwr_cfg_0, ldo_cfg0, bbp1,
                rfcsr1, rfcsr2, rfcsr3, rfcsr6, rfcsr12, rfcsr13,
            )
        except (IOError, usb.core.USBError) as e:
            logger.debug("[%s] register read failed: %s", tag, e)

    def _dump_tx_counters(self, tag: str) -> None:
        """Read TX_STA_CNT0/1 + drain TX_STA_FIFO. CNT0/1 are read-to-clear
        aggregates. TX_STA_FIFO is the per-frame status FIFO — each read
        pops one entry. VALID bit (0x1) set means a real entry; if we get
        all-zero entries, the FIFO is empty (= no frames actually TX'd by
        chip). Drain up to 8 entries per call."""
        from .constants import TX_STA_CNT0, TX_STA_CNT1, TX_STA_FIFO
        try:
            cnt0 = self.transport.read32(TX_STA_CNT0)
            cnt1 = self.transport.read32(TX_STA_CNT1)
            # TX_STA_CNT0: bits[15:0] = TX_FAIL_COUNT, bits[31:16] = TX_BEACON_COUNT
            # TX_STA_CNT1: bits[15:0] = TX_SUCCESS_COUNT, bits[31:16] = TX_RETRANSMIT_COUNT
            # [SRC] rt2800.h:1889-1898
            tx_fail = cnt0 & 0xFFFF
            tx_beacon = (cnt0 >> 16) & 0xFFFF
            tx_ok = cnt1 & 0xFFFF
            tx_retry = (cnt1 >> 16) & 0xFFFF
            fifo_entries = []
            for _ in range(8):
                entry = self.transport.read32(TX_STA_FIFO)
                if not (entry & 0x1):
                    break
                fifo_entries.append(entry)
            logger.debug(
                "[%s] CNT0=0x%08x CNT1=0x%08x "
                "→ ok=%d fail=%d retry=%d beacon=%d | FIFO drained %d: %s",
                tag, cnt0, cnt1, tx_ok, tx_fail, tx_retry, tx_beacon,
                len(fifo_entries),
                " ".join(f"0x{e:08x}" for e in fifo_entries) or "(empty)",
            )
        except (IOError, usb.core.USBError) as e:
            logger.debug("[%s] TX counter read failed: %s", tag, e)

    async def enter_active_monitor(self, mac: bytes, bssid: Optional[bytes] = None) -> bytes:
        """Program ``mac`` as the self-MAC with UNICAST_TO_ME_MASK=0xff so the
        autoresponder HW-ACKs frames to it. RX stays promiscuous and the AP's replies
        are addressed to ``mac``, so they still arrive. Reversed by exit_active_monitor."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: write_mac_address(self.transport, bytes(mac), u2me_mask=0xFF),
        )
        return bytes(mac)

    async def exit_active_monitor(self) -> None:
        """Restore the monitor baseline: re-program the real MAC with
        UNICAST_TO_ME_MASK=0 (promiscuous capture, autoresponder matches nothing)."""
        if self._eeprom is None:
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: write_mac_address(self.transport, self._eeprom.mac_address),
        )

    async def close(self) -> None:
        if self._link_tuner_task is not None:
            self._link_tuner_task.cancel()
            try:
                await self._link_tuner_task
            except asyncio.CancelledError:
                pass
            self._link_tuner_task = None
        if self._rx_reader is not None:
            await self._rx_reader.stop()
            self._rx_reader = None
        self._release()
        logger.info("rt2800usb driver closed")
