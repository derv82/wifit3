"""rt2800usb driver — Panda PAU05 (RT5372) / Panda PAU09 (RT5572) /
ALFA AWUS051NH v2 (RT3572).

  * RT5372 (silicon RT5392): 2.4 GHz, 1T1R — DONE.
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

from wifit3.engine.protocols import DeviceID, ProgressCallback

from .constants import (
    EEPROM_NIC_CONF1_ANT_DIVERSITY_MASK,
    EEPROM_NIC_CONF1_ANT_DIVERSITY_SHIFT,
    RT_RT5592,
    USB_PID_RT3572,
    USB_PID_RT5372,
    USB_PID_RT5572,
    USB_VID_RALINK,
)
from wifit3.wlan.packet import WlanFrameParser

from .bbp import init_bbp, prepare_bbp
from .chan import CHANNELS_5G_NON_DFS, is_xtal_40mhz, set_channel as _set_channel
from .eeprom import parse_eeprom, read_eeprom_efuse
from .firmware import load_firmware, load_firmware_blob
from .mac import (
    ChipId, enable_radio, is_chip_warm, read_chip_id, read_perm_mac,
    usb_init_registers, write_mac_address,
)
from .reg_init import init_registers
from .rfcsr import RfFilterCal, init_rfcsr
from .rx import parse_rx_urb, probe_endpoints, read_rx_burst, rxwi_size_for_silicon
from .transport import RT2800USBTransport
from .tx import inject_frame as _inject_frame, txwi_size_for_silicon

logger = logging.getLogger(__name__)


class RT2800USBDriver:
    """Driver for the rt2800usb family (RT3572 / RT5372 / RT5572).

    Per-variant differences (RX/TX desc size, RF init, 5 GHz support)
    are dispatched at runtime via the ``chip_id`` carried in DeviceID
    extras + the silicon ID read from MAC_CSR0 at connect() time.
    """

    SUPPORTED_IDS = [
        DeviceID(USB_VID_RALINK, USB_PID_RT5372,
                 "Ralink RT5372 / Panda PAU05",
                 extras={"chip_id": "rt5372"}),
        DeviceID(USB_VID_RALINK, USB_PID_RT3572,
                 "Ralink RT3572 / ALFA AWUS051NH v2",
                 extras={"chip_id": "rt3572"}),
        DeviceID(USB_VID_RALINK, USB_PID_RT5572,
                 "Ralink RT5572 / Panda PAU09 N600",
                 extras={"chip_id": "rt5572"}),
    ]
    # 2.4 GHz channels 1..13 are claimed by all three chips. 5 GHz
    # channels are advertised on the class so the scanner / hopper
    # picks them up for RT3572 + RT5572; RT5392 will fail-soft if the
    # hopper asks it to tune one. (See chan.set_channel: RT5392 raises
    # ValueError for ch > 14 and driver.set_channel returns False.)
    SUPPORTED_CHANNELS = list(range(1, 14)) + list(CHANNELS_5G_NON_DFS)

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RT2800USBDriver":
        chip_id_hint = id_entry.extras.get("chip_id", "")
        return cls(dev, chip_id_hint=chip_id_hint)

    def __init__(self, dev: usb.core.Device, *, chip_id_hint: str = ""):
        self.dev = dev
        self.transport = RT2800USBTransport(dev)
        self._rx_callback: Optional[Callable[[dict], None]] = None
        self._rx_task: Optional[asyncio.Task] = None
        self._rx_running = False
        self._bulk_in_ep: Optional[int] = None
        self._rxwi_size: int = 16          # set at connect-time from silicon_id
        self._claimed = False
        self._eeprom = None                 # EepromValues post-EFUSE-read
        # RT3572-only: filter calibration values + saved BBP25/26 from
        # init_rfcsr_3572, replayed on every channel tune.
        self._rf_cal: Optional[RfFilterCal] = None

        # WlanDriver Protocol surface area.
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self.current_channel: int = 1
        self.chip_id: Optional[ChipId] = None
        self.chip_id_hint = chip_id_hint   # from VID:PID; e.g. "rt5372"
        # RT5592-only: probed at connect() time from MAC_DEBUG_INDEX.XTAL.
        # Picks which of rf_vals_5592_xtal20 / xtal40 the channel tune
        # consults (PAU09 N600's actual xtal isn't documented).
        self._xtal_40mhz: bool = False

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

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
                logger.error(
                    "silicon ID 0x%04x not in M1 supported set "
                    "(RT3572, RT5390, RT5592)",
                    self.chip_id.silicon_id,
                )
                return False

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
                logger.error("firmware load failed: %s", e)
                return False

            _progress(0.95, "Verifying post-FW state (PBF.READY)")
            from .constants import PBF_SYS_CTRL, PBF_SYS_CTRL_READY
            pbf = await loop.run_in_executor(None, self.transport.read32, PBF_SYS_CTRL)
            if not (pbf & PBF_SYS_CTRL_READY):
                logger.error(
                    "post-FW PBF.READY not set (PBF_SYS_CTRL=0x%08x)", pbf
                )
                return False
            logger.info("post-FW PBF_SYS_CTRL=0x%08x — READY latched", pbf)

            _progress(0.95, "Running rt2800usb_init_registers (M2b-1)")
            try:
                await loop.run_in_executor(None, usb_init_registers, self.transport)
            except (IOError, usb.core.USBError) as e:
                logger.error("usb_init_registers failed: %s", e)
                return False

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
                logger.error("EFUSE read failed: %s", e)
                return False

            _progress(0.96, "Running rt2800_init_registers (M2b-2 MAC config)")
            try:
                await loop.run_in_executor(
                    None,
                    lambda: init_registers(self.transport, self.chip_id.silicon_id),
                )
            except (IOError, usb.core.USBError) as e:
                logger.error("init_registers failed: %s", e)
                return False

            _progress(0.965, "Preparing BBP (MCU_BOOT_SIGNAL + wait_bbp_ready)")
            try:
                await loop.run_in_executor(None, prepare_bbp, self.transport)
            except (IOError, usb.core.USBError) as e:
                logger.error("prepare_bbp failed: %s", e)
                return False

            # Path counts from EEPROM (RT5392 hw is always 1T1R so it
            # ignores these; RT3572 hw is typically 2T2R, EFUSE-derived).
            # EepromValues.{tx,rx}path handles the unburned-EFUSE case
            # (0x0000 or 0xFFFF NIC_CONF0) by returning kernel defaults
            # (2 RX / 1 TX) so we don't power down the wrong chains.
            txpath = self._eeprom.txpath if self._eeprom else 1
            rxpath = self._eeprom.rxpath if self._eeprom else 1
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
                logger.error("init_bbp failed: %s", e)
                return False

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
                logger.error("init_rfcsr failed: %s", e)
                return False
            if self._rf_cal is not None:
                logger.info(
                    "RF filter cal: bw20=0x%02x bw40=0x%02x bbp25=0x%02x bbp26=0x%02x",
                    self._rf_cal.calibration_bw20, self._rf_cal.calibration_bw40,
                    self._rf_cal.bbp25, self._rf_cal.bbp26,
                )

            _progress(0.985, "Enabling radio (MAC TX/RX + WPDMA + USB DMA)")
            try:
                await loop.run_in_executor(
                    None,
                    lambda: enable_radio(self.transport, self.chip_id.silicon_id),
                )
            except (IOError, usb.core.USBError) as e:
                logger.error("enable_radio failed: %s", e)
                return False

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
            self._rx_running = True
            self._rx_task = asyncio.create_task(self._rx_loop())

            self.is_warm = True
            return True

        except (IOError, usb.core.USBError, NotImplementedError) as e:
            logger.error("rt2800usb M1 connect failed: %s", e)
            return False

    # ---- RX loop --------------------------------------------------------
    async def _rx_loop(self) -> None:
        loop = asyncio.get_event_loop()
        ep = self._bulk_in_ep
        assert ep is not None
        logger.info("rt2800usb RX loop started on EP 0x%02x (RXWI=%dB)",
                    ep, self._rxwi_size)
        while self._rx_running:
            try:
                buf = await loop.run_in_executor(None, read_rx_burst, self.dev, ep)
            except usb.core.USBError as e:
                logger.error("rt2800usb bulk-IN error: %s", e)
                await asyncio.sleep(0.05)
                continue
            except Exception as e:
                logger.exception("rt2800usb RX loop unexpected error: %s", e)
                await asyncio.sleep(0.05)
                continue
            if buf is None:
                continue
            rx = parse_rx_urb(buf, rxwi_size=self._rxwi_size)
            if rx is None or rx.has_fcs_error:
                continue
            parsed = WlanFrameParser.parse_80211_frame(rx.mpdu, rx.rssi_dbm)
            if parsed is not None and self._rx_callback is not None:
                try:
                    self._rx_callback(parsed)
                except Exception as e:
                    logger.exception("rx_callback raised: %s", e)
        logger.info("rt2800usb RX loop stopped")

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
            kwargs.update(
                cal_result=self._rf_cal,
                tx_chain_num=self._eeprom.txpath,
                rx_chain_num=self._eeprom.rxpath,
                has_cap_bt_coexist=self._eeprom.has_cap_bt_coexist,
                has_cap_external_lna_a=self._eeprom.has_cap_external_lna_a,
            )
        elif self.chip_id is not None and self.chip_id.silicon_id == RT_RT5592:
            kwargs.update(
                tx_chain_num=self._eeprom.txpath,
                rx_chain_num=self._eeprom.rxpath,
                has_cap_bt_coexist=self._eeprom.has_cap_bt_coexist,
                xtal_40mhz=self._xtal_40mhz,
            )
        return kwargs

    async def set_channel(self, channel: int) -> bool:
        if self.chip_id is None:
            logger.error("set_channel(%d): connect() must run first", channel)
            return False
        kwargs = self._channel_kwargs(channel)
        try:
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
        return True

    # ---- TX inject (M5) -------------------------------------------------
    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        if self.chip_id is None:
            logger.error("inject_frame: connect() must run first")
            return False
        # rt2800 uses TXWI size that varies by silicon — pre-compute.
        txwi_sz = txwi_size_for_silicon(self.chip_id.silicon_id)
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: _inject_frame(
                    self.dev, frame_bytes,
                    txwi_size=txwi_sz, use_no_ack=use_no_ack,
                ),
            )
            return True
        except ValueError as e:
            logger.warning("rt2800usb inject_frame bad frame: %s", e)
            return False
        except usb.core.USBError as e:
            logger.error("rt2800usb inject_frame USBError: %s", e)
            return False

    async def close(self) -> None:
        self._rx_running = False
        if self._rx_task:
            self._rx_task.cancel()
            try:
                await self._rx_task
            except asyncio.CancelledError:
                pass
        self._release()
        logger.info("rt2800usb driver closed")
