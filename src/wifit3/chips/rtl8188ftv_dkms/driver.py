"""RTL8188FTV DKMS (vendor) driver — scaffold.

Cleanroom port of ``kelebek333/rtl8188fu`` (``v4.3.23.6_20964.20170110``,
RTL871X stack, ``CONFIG_RTL8188F``) for the no-name ``0bda:f179`` dongles.
Nothing is ported yet: every runtime method raises until its milestone lands
and ``scripts/chips/rtl8188ftv_dkms/verify_pcap.py`` replays it byte-for-byte
against ``driver_captures/captures_8188fu/capture-1.pcap``. Status and the
milestone map live in ``RTL8188FTV_DKMS.md``.
"""
from __future__ import annotations

from typing import Callable, ClassVar, List, Optional

import usb.core

from wifit3.chips.driver import Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.models.device_id import DeviceID

_NOT_PORTED = (
    "RTL8188FTV DKMS port not yet ported; "
    "set WIFIT3_RTL8188FTV=mainline to use the working mainline port"
)


class Rtl8188ftvDkmsDriver(Driver):
    SUPPORTED_CHANNELS: ClassVar[List[int]] = list(range(1, 14))
    FAKE_MAC: ClassVar[FakeMacSupport] = FakeMacSupport.NONE

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "Rtl8188ftvDkmsDriver":
        return cls(dev)

    def __init__(self, dev: usb.core.Device):
        super().__init__()
        self.dev = dev
        self.mac_address: Optional[str] = None
        self.is_warm: bool = False
        self._rx_callback: Optional[Callable] = None
        self._on_lost: Optional[Callable] = None

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable) -> None:
        self._on_lost = cb

    async def connect(self, progress_cb: Optional[ProgressCallback] = None) -> bool:
        raise BringUpError("init", _NOT_PORTED)

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        raise BringUpError("tune", _NOT_PORTED)

    async def close(self) -> None:
        return None

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        raise BringUpError("inject", _NOT_PORTED)

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        return frame_bytes

    async def _enable_rx_acks(self) -> None:
        return None

    async def _disable_rx_acks(self) -> None:
        return None
