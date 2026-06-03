"""RTL8814AU driver — vendor (morrownr DKMS) cleanroom port.

Status: M1 (firmware upload + FW-ready ACK). Power-on, LLT, and the 3081/IDDMA
firmware download are complete and pcap-verified; PHY/MAC/RF init, channel tune,
RX and TX are later milestones and the corresponding methods raise until then.

This driver is intentionally NOT registered in ``wlan/manager.py`` yet — master
keeps the working mainline-derived ``rtw88_8814au`` port until this vendor port is
hardware-proven to beat it. Exercise M1 via ``scripts/rtl8814au_dkms/``.
"""
from __future__ import annotations

import asyncio
import logging
from importlib import resources
from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.engine.protocols import DeviceID, ProgressCallback

from .constants import PID_RTL8814AU, VID_REALTEK
from .firmware import bring_up
from .mac import phy_mac_config
from .transport import Rtl8814auTransport

logger = logging.getLogger(__name__)

_FW_ASSET = "rtl8814au_fw.bin"


def _load_firmware() -> bytes:
    return (resources.files(__package__) / "assets" / _FW_ASSET).read_bytes()


class Rtl8814auDkmsDriver:
    SUPPORTED_IDS: ClassVar[List[DeviceID]] = [
        DeviceID(VID_REALTEK, PID_RTL8814AU,
                 "Realtek RTL8814AU 4T4R (ALFA AWUS1900) — vendor/DKMS port"),
    ]
    # 20 MHz primary on every band the card supports. [WIRE] capture hop list.
    SUPPORTED_CHANNELS: ClassVar[List[int]] = (
        list(range(1, 14))
        + [36, 40, 44, 48, 52, 56, 60, 64]
        + [100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144]
        + [149, 153, 157, 161, 165]
    )

    def __init__(self, transport: Rtl8814auTransport):
        self.transport = transport
        self.mac_address: Optional[str] = None  # M2: efuse read
        self.is_warm: bool = False
        self._rx_cb: Optional[Callable[[dict], None]] = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8814auDkmsDriver":
        return cls(Rtl8814auTransport(dev))

    def register_rx_callback(self, cb: Callable[[dict], None]) -> None:
        self._rx_cb = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        if progress_cb:
            progress_cb(0.0, "Loading RTL8814AU firmware")
        fw = _load_firmware()
        if progress_cb:
            progress_cb(0.2, "Uploading firmware (3081 IDDMA)")
        loop = asyncio.get_running_loop()
        # Bring-up does blocking synchronous USB I/O; keep it off the event loop.
        ready = await loop.run_in_executor(None, bring_up, self.transport, fw)
        if not ready:
            logger.error("RTL8814AU firmware download did not reach CPU_DL_READY")
            if progress_cb:
                progress_cb(1.0, "Firmware NOT ready")
            return False
        # M2a: MAC register table. (Extend this chain as later milestones land;
        # keep it in sync with scripts/rtl8814au_dkms/verify_pcap.py.)
        if progress_cb:
            progress_cb(0.7, "Configuring MAC registers")
        await loop.run_in_executor(None, phy_mac_config, self.transport)
        if progress_cb:
            progress_cb(1.0, "MAC configured")
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        raise NotImplementedError("RTL8814AU DKMS port: channel tune is M2+")

    async def inject_frame(self, frame_bytes: bytes, use_no_ack: bool = True) -> bool:
        raise NotImplementedError("RTL8814AU DKMS port: TX is a later milestone")

    async def close(self) -> None:
        self.transport.close()
