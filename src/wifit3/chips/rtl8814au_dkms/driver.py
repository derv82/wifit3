"""RTL8814AU driver — vendor (morrownr DKMS) cleanroom port.

Status: 2.4 GHz RX + TX complete and hardware-verified. ``connect()`` runs the full
deterministic init (EFUSE -> firmware -> MAC/BB/RF -> channel tune -> TX power ->
InitHalDm seed -> hal_init turn-on tail -> monitor opmode entry), all pcap-verified,
then starts the bulk-IN RX reader (promiscuous monitor frames + per-frame RSSI) and
the runtime phydm DIG/AGC watchdog. ``inject_frame`` builds the mgmt TX descriptor and
transmits — deauth (M4c) and WEP ARP replay (M4d) are live-verified, and monitor RX is
confirmed promiscuous in both directions (captures client->AP, incl. WPA M2/M4).
5 GHz @ 20 MHz is ported (M5a band switch / M5b channel select / M5c runtime / M5d TX
power — ``set_channel`` tunes 2.4 GHz + 5 GHz with correct per-rate TX power). Pending:
the ch153 spur notch (M5f, minor RX polish).

This driver is intentionally NOT registered in ``wlan/manager.py`` yet — master
keeps the working mainline-derived ``rtw88_8814au`` port until this vendor port is
hardware-proven to beat it on 2.4 GHz breadth. Exercise it via ``scripts/rtl8814au_dkms/``.
"""
from __future__ import annotations

import asyncio
import logging
from importlib import resources
from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.engine.protocols import DeviceID, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from ..rx_reader import RxReaderThread
from .bb import phy_bb_config
from .chan import init_tune, set_channel_bw, set_rfe_reg_init
from .constants import BBSWING_DEFAULT, CHANNELS_2G, CHANNELS_5G, PID_RTL8814AU, VID_REALTEK
from .dig import WATCHDOG_PERIOD_S, watchdog_tick
from .dm import init_hal_dm
from .efuse import read_chip_params
from .firmware import bring_up
from .mac import hal_init_turn_on, mac_init_misc, phy_mac_config
from .monitor import enter_monitor
from .rf import phy_rf_config
from .rx import iter_frames
from .transport import Rtl8814auTransport
from .tx import build_mgmt_txdesc

logger = logging.getLogger(__name__)

_FW_ASSET = "rtl8814au_fw.bin"
_DEFAULT_CHANNEL = 1  # connect-time tune target (matches the cold-boot capture)


def _load_firmware() -> bytes:
    return (resources.files(__package__) / "assets" / _FW_ASSET).read_bytes()


class Rtl8814auDkmsDriver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(VID_REALTEK, PID_RTL8814AU,
                 "Realtek RTL8814AU 4T4R (ALFA AWUS1900) — vendor/DKMS port"),
    ]
    # 2.4 GHz + 5 GHz, 20 MHz primary (M5a band switch / M5b select / M5c runtime / M5d TX
    # power) — both bands tune with correct per-rate TX power for RX and inject.
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(CHANNELS_2G + CHANNELS_5G)

    def __init__(self, transport: Rtl8814auTransport):
        self.transport = transport
        self.mac_address: Optional[str] = None  # M2: efuse read
        self._channel: Optional[int] = None
        self._tx_power: tuple = ()  # per-path efuse TX-power info, 2.4 GHz (M2e)
        self._tx_power_5g: tuple = ()  # per-path efuse TX-power info, 5 GHz (M5d)
        # Per-path BB-swing (TxScale) per band — phy_SetBBSwingByBand on a band switch.
        # Both bands are efuse-decoded (2.4 GHz M4e / efuse 0xC6, 5 GHz M5e / efuse 0xC7).
        self._bb_swing_2g: tuple = (BBSWING_DEFAULT,) * 4
        self._bb_swing_5g: tuple = (BBSWING_DEFAULT,) * 4
        self.is_warm: bool = False
        # Runtime DIG/AGC watchdog (M3c). Toggleable so a fixed-channel A/B can
        # isolate the watchdog's effect on RX breadth (scan_hw.py --no-dig).
        self.enable_dig: bool = True
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._dig_task: Optional[asyncio.Task] = None
        # Serializes control-transfer batches (DIG watchdog vs set_channel) so two
        # executor threads never drive EP0 at once; the RX reader uses bulk-IN.
        self._io_lock = asyncio.Lock()

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8814auDkmsDriver":
        return cls(Rtl8814auTransport(dev))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()
        # All bring-up does blocking synchronous USB I/O; keep it off the event loop.

        # EFUSE read (vendor probe order: before _InitPowerOn). Yields rfe_type
        # (BB phy_cond discriminator), crystal_cap, and the MAC address.
        if progress_cb:
            progress_cb(0.0, "Reading EFUSE / chip parameters")
        params = await loop.run_in_executor(None, read_chip_params, self.transport)
        self.mac_address = params.mac_address
        self._tx_power = params.tx_power
        self._tx_power_5g = params.tx_power_5g
        self._bb_swing_2g = params.bb_swing
        self._bb_swing_5g = params.bb_swing_5g
        logger.info("RTL8814AU efuse: rfe_type=%d crystal_cap=0x%02x mac=%s bb_swing=%s",
                    params.rfe_type, params.crystal_cap,
                    params.mac_address or "<none>",
                    "/".join(f"0x{v:03x}" for v in params.bb_swing))

        if progress_cb:
            progress_cb(0.2, "Uploading firmware (3081 IDDMA)")
        fw = _load_firmware()
        ready = await loop.run_in_executor(None, bring_up, self.transport, fw)
        if not ready:
            logger.error("RTL8814AU firmware download did not reach CPU_DL_READY")
            if progress_cb:
                progress_cb(1.0, "Firmware NOT ready")
            return False

        # Deterministic init chain M2a -> M3b-2 (all pcap-verified). Keep it in sync
        # with scripts/rtl8814au_dkms/verify_pcap.py.
        if progress_cb:
            progress_cb(0.7, "Configuring MAC / BB / RF registers")

        def _phy_config(t):
            phy_mac_config(t)     # M2a: MAC register table
            mac_init_misc(t)      # M2b: hal_init MISC stage
            phy_bb_config(t, params.rfe_type, params.crystal_cap)  # M2b: PHY_BBConfig8814
            phy_rf_config(t, params.rfe_type)                      # M2c: PHY_RFConfig8814A
            init_tune(t, _DEFAULT_CHANNEL, params.tx_power, params.tx_power_5g,
                      self._bb_swing_2g, self._bb_swing_5g)  # M2d/M2e: ch tune + TX power
            init_hal_dm(t)                                         # M3a: InitHalDm DIG/AGC seed
            set_rfe_reg_init(t, params.rfe_type)                   # M3b-1: PHY_SetRFEReg8814A(TRUE)
            hal_init_turn_on(t, self.mac_address)                  # M3b-1: turn-on tail + MAC addr
            enter_monitor(t)                                       # M3b-2: monitor opmode (RCR/RXFLTMAP)

        await loop.run_in_executor(None, _phy_config, self.transport)
        self._channel = _DEFAULT_CHANNEL

        # M3b-3a: start the bulk-IN RX reader. It keeps a blocking bulk read posted
        # on a dedicated thread (off the event loop, so the TUI can't starve RX);
        # each aggregated buffer is split into 802.11 frames and fanned to the rx
        # callback. RSSI is a placeholder until the PHY-status decode (M3b-3b).
        self._reader = RxReaderThread(
            loop, self._read_once, self._dispatch, name="8814au-dkms-rx")
        self._reader.start()

        # M3c: the runtime phydm DIG/AGC watchdog — adapt the M3a IGI seed to the
        # live false-alarm rate every ~2 s (the kernel cadence). RX-side only.
        if self.enable_dig:
            self._dig_task = loop.create_task(self._dig_watchdog())
        else:
            logger.info("RTL8814AU DIG watchdog disabled (IGI stays at the M3a seed)")

        if progress_cb:
            progress_cb(1.0, f"Tuned to channel {_DEFAULT_CHANNEL} @ 20 MHz")
        return True

    async def _dig_watchdog(self) -> None:
        """Periodic DIG watchdog (M3c). Serialized with set_channel via _io_lock."""
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(WATCHDOG_PERIOD_S)
                async with self._io_lock:
                    tick = await loop.run_in_executor(None, watchdog_tick, self.transport)
                logger.debug("RTL8814AU DIG: IGI=0x%02x fa=%d (ofdm=%d cck=%d)",
                             tick.igi, tick.fa_cnt, tick.ofdm_fa, tick.cck_fa)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 — a watchdog fault must not kill RX
            logger.exception("RTL8814AU DIG watchdog stopped on error")

    # --- RX path (M3b-3a) --------------------------------------------------
    def _read_once(self) -> Optional[bytes]:
        """Reader-thread side: one blocking bulk-IN read (None on no traffic)."""
        return self.transport.bulk_in()

    def _dispatch(self, buf: bytes) -> None:
        """Loop side: split the aggregated bulk-IN buffer into (frame, rssi) pairs
        and fan each parsed dict to the rx callback. FCS already stripped."""
        cb = self._rx_cb
        if cb is None:
            return
        for frame, rssi in iter_frames(buf):
            parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
            if parsed is not None:
                cb(parsed)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        """Tune to a 2.4 GHz or 5 GHz channel at 20 MHz (band-switches on a crossing).

        Sets the per-rate TX power for the channel's band (M2e / M5d), so both RX and
        inject/deauth use correct power on either band.
        """
        loop = asyncio.get_running_loop()
        async with self._io_lock:   # don't race the DIG watchdog's control I/O
            await loop.run_in_executor(
                None, set_channel_bw, self.transport, channel, self._tx_power,
                self._tx_power_5g, self._bb_swing_2g, self._bb_swing_5g)
        self._channel = channel
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """Transmit one 802.11 management frame (e.g. a deauth).

        Builds the management TX descriptor (M4a) and sends ``[desc | frame]`` on the
        bulk-OUT pipe — the same RtOutPipe[0] the firmware download uses, which is where
        the MGMT queue maps. ``frame_bytes`` is the MPDU *without* FCS (the HW appends
        it). Serialized with ``set_channel`` / the DIG watchdog via ``_io_lock`` so the
        frame is never emitted mid-retune. TX is explicit-action only (passive-by-
        default): nothing on the scan/connect path calls this.

        ``use_no_ack`` is accepted for API compatibility; the minimal mgmt descriptor
        uses the HW-default ACK/retry policy for now (revisit when deauth is exercised
        live in M4c). TX-FIFO/queue prerequisites are covered by the M2b MISC stage
        (MACTXEN + queue/page) — to confirm on the live smoke test.
        """
        if len(frame_bytes) < 10:           # need addr1 (bytes [4:10]) to read BMC
            return False
        loop = asyncio.get_running_loop()
        bmc = bool(frame_bytes[4] & 0x01)   # addr1 group-address (multicast) bit
        desc = build_mgmt_txdesc(len(frame_bytes), bmc=bmc)
        async with self._io_lock:           # don't TX mid-retune (set_channel/DIG)
            await loop.run_in_executor(
                None, self.transport.bulk_out, desc + frame_bytes)
        return True

    async def close(self) -> None:
        # Stop the DIG watchdog and the reader before releasing the USB handle.
        if self._dig_task is not None:
            self._dig_task.cancel()
            try:
                await self._dig_task
            except asyncio.CancelledError:
                pass
            self._dig_task = None
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        self.transport.close()
