"""AR9271 (ath9k_htc) driver — clean-room v2 re-port from the v6.18.12 kernel source.

A fresh bring-up against the mainline ``htc_9271-1.4.0.fw`` protocol, verified op-by-op
against the cold-boot pcap (``scripts/ar9271_v2/verify_pcap.py``).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, ClassVar, List, Optional

import libusb_package
import usb.core

from wifit3.engine.protocols import DeviceID, FakeMacSupport, ProgressCallback

from . import constants as C, firmware, htc
from .transport import AR9271Transport

logger = logging.getLogger(__name__)


class AR9271V2Driver:
    """Atheros AR9271 / ALFA AWUS036NHA — 2.4 GHz, soft-MAC, ath9k_htc firmware."""

    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(C.AR9271_VID, C.AR9271_PID, "Atheros AR9271 / ALFA AWUS036NHA (v2)"),
    ]
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 14))   # 2.4 GHz only, no 5 GHz radio
    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.UNIMPLEMENTED

    def __init__(self, dev: usb.core.Device):
        self.transport = AR9271Transport(dev)
        self.is_warm = False
        self.mac_address: Optional[str] = None
        self.htc: Optional[htc.HTCState] = None
        self._rx_callback: Optional[Callable[[dict], None]] = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "AR9271V2Driver":
        return cls(dev)

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_callback = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        def _p(pct: float, msg: str) -> None:
            if progress_cb:
                progress_cb(pct, msg)
            logger.info("ar9271_v2 %d%%: %s", int(pct * 100), msg)

        _p(0.05, "Downloading AR9271 firmware...")
        fw = firmware.load_firmware_blob()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, firmware.download, self.transport, fw)

        _p(0.30, "Waiting for AR9271 to re-enumerate...")
        warm = await self._await_reenumeration()
        if warm is None:
            logger.error("ar9271_v2: card did not re-enumerate after firmware download")
            return False
        self.transport = AR9271Transport(warm)

        _p(0.45, "HTC/WMI handshake...")
        self.htc = await loop.run_in_executor(None, htc.handshake, self.transport)

        # M2b+: WMI register init (ath9k_hw), calibration, monitor RX filter. Not yet ported
        # — fail loudly rather than report a half-initialised card as ready.
        raise NotImplementedError("ar9271_v2: M2b (WMI register init) not yet ported")

    async def _await_reenumeration(self) -> Optional[usb.core.Device]:
        backend = libusb_package.get_libusb1_backend()
        for _ in range(12):                 # ~3 s; the chip boots its text image and re-attaches
            await asyncio.sleep(0.25)
            dev = usb.core.find(idVendor=C.AR9271_VID, idProduct=C.AR9271_PID, backend=backend)
            if dev is not None:
                return dev
        return None

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        raise NotImplementedError("ar9271_v2: set_channel lands with M3")

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        raise NotImplementedError("ar9271_v2: inject_frame lands with M5")

    async def close(self) -> None:
        try:
            usb.util.dispose_resources(self.transport.dev)
        except Exception:
            pass
