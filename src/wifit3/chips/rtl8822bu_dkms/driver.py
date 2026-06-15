"""RTL8822BU / 2T2R driver — vendor (HALMAC/PHYDM) cleanroom port.

`connect()` runs the deterministic cold bring-up that the byte-for-byte gate verifies
(`scripts/rtl8822bu_dkms/verify_pcap.py` → `bringup.cold_bringup`): chip-ID/USB-PHY → EFUSE →
two-cycle power/FW/MAC init → BB phy-reg/AGC + crystal + RF-A/RF-B tables. It then tunes to the
default channel (`chan.set_channel_bw`, byte-verified against the capture's airodump hops), starts
the bulk-IN RX reader, and opens the monitor RCR.

**RX status (hardware, 2026-06-15):** cold init + `set_channel` run clean on the card (RF tunes to
the target channel, both radios respond). The monitor RX path is fully wired (RCR=0x9000380F,
RXFLTMAP=0xFFFF, bulk-IN 0x84 reader, FCS-stripped rx-desc decode), and the BB hears RF energy on
a live channel (the FA/CCA counters climb). But **no frame yet completes into the RXFF**, so
beacons are not delivered. The blocker is the post-init RX seed the vendor runs after the BB/RF
tables (the phydm DM init / RX cal — CCK-PD + DIG + the RX demod path), which is the next
milestone. See RTL8822BU_DKMS.md "RX bring-up". No RF calibration (IQK/DPK) is ported yet either.

Not registered in the manager (the mainline `chips/rtl8822bu/` owns 2357:0138); this `_dkms` port
is exercised standalone via `scripts/rtl8822bu_dkms/test_hw.py`. `inject_frame` is a stub — TX (M8)
is a later milestone, and the agent never fires live TX.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, List, Optional

import usb.core
import usb.util

from wifit3.engine.protocols import DeviceID, ProgressCallback
from wifit3.wlan.packet import WlanFrameParser

from ..rx_reader import RxReaderThread
from . import bringup, chan
from .constants import REG_RCR
from .rx import iter_frames
from .transport import Rtl8822buTransport

logger = logging.getLogger(__name__)

USB_VID_REALTEK = 0x2357
USB_PID_T3U_PLUS = 0x0138
_DEFAULT_CHANNEL = 1
_BULK_OUT_EP_TX = 0x05                          # 8822b bulk-OUT (FW/TX)
RCR_MONITOR_VALUE = 0x9000380F                  # AAP|APM|AM|AB|ADF|ACF|AMF|APP_PHYST|APPFCS
CHANNELS_2G = list(range(1, 14))
CHANNELS_5G = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124,
               128, 132, 136, 140, 144, 149, 153, 157, 161, 165]


class Rtl8822buDkmsDriver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(USB_VID_REALTEK, USB_PID_T3U_PLUS,
                 "Realtek RTL8822BU 2T2R (TP-Link Archer T3U Plus) — vendor/DKMS port"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = CHANNELS_2G + CHANNELS_5G

    def __init__(self, transport: Rtl8822buTransport):
        self.transport = transport
        self.mac_address: Optional[str] = None
        self._chip = None                       # (info, efuse) from cold_bringup
        self._channel: Optional[int] = None
        self._rx_cb: Optional[Callable[[dict], None]] = None
        self._reader: Optional[RxReaderThread] = None
        self._io_lock = asyncio.Lock()

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8822buDkmsDriver":
        return cls(Rtl8822buTransport(dev, bulk_out_ep=_BULK_OUT_EP_TX))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    def _claim(self) -> None:
        """Detach kernel driver / configure / claim interface 0 (OS-level USB plumbing —
        outside the vendor op stream the gate reproduces)."""
        dev = self.transport.dev
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError) as e:
            logger.debug("kernel-driver detach skipped: %s", e)
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            raise IOError(f"set_configuration failed: {e}") from e
        usb.util.claim_interface(dev, 0)

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._claim)

        if progress_cb:
            progress_cb(0.1, "Cold bring-up: chip-ID / EFUSE / FW / MAC / BB / RF")
        info, e = await loop.run_in_executor(None, bringup.cold_bringup, self.transport)
        self._chip = (info, e)
        logger.info("RTL8822BU cold init done: cut=%d rfe_type=%d crystal_cap=0x%02x",
                    info.chip_ver, e.rfe_type, e.crystal_cap)

        if progress_cb:
            progress_cb(0.8, f"Tuning to channel {_DEFAULT_CHANNEL} @ 20 MHz")
        await loop.run_in_executor(None, chan.set_channel_bw, self.transport, _DEFAULT_CHANNEL)
        self._channel = _DEFAULT_CHANNEL

        # Start the bulk-IN reader before opening the RX gate (an undrained pipe wedges RX FIFO).
        self._reader = RxReaderThread(loop, self._read_once, self._dispatch, name="8822bu-dkms-rx")
        self._reader.start()
        await loop.run_in_executor(None, self.transport.write32, REG_RCR, RCR_MONITOR_VALUE)

        if progress_cb:
            progress_cb(1.0, f"Tuned to channel {_DEFAULT_CHANNEL} @ 20 MHz (monitor)")
        return True

    # --- RX path -----------------------------------------------------------
    def _read_once(self) -> Optional[bytes]:
        return self.transport.bulk_in()

    def _dispatch(self, buf: bytes) -> None:
        cb = self._rx_cb
        if cb is None:
            return
        for frame, rssi in iter_frames(buf):
            parsed = WlanFrameParser.parse_80211_frame(frame, rssi)
            if parsed is not None:
                cb(parsed)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        loop = asyncio.get_running_loop()
        prev = self._channel

        def _tune(t):
            chan.set_channel_bw(t, channel, prev_ch=prev)

        async with self._io_lock:
            await loop.run_in_executor(None, _tune, self.transport)
        self._channel = channel
        return True

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        """TX is a later milestone (M8); the agent never fires live TX."""
        logger.warning("RTL8822BU inject_frame: TX not yet ported (M8)")
        return False

    async def close(self) -> None:
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None
        try:
            usb.util.release_interface(self.transport.dev, 0)
        except usb.core.USBError as e:
            logger.debug("release_interface(0): %s", e)
        self.transport.close()
