from __future__ import annotations

import asyncio
from typing import Callable

import usb.core

from wifit3.chips.driver import Driver, FakeMacSupport, ProgressCallback
from wifit3.errors import BringUpError
from wifit3.models.device_id import DeviceID

from .hw import rtl92cu_hw_init
from .priv import RtlPriv
from .transport import RTL8192CUTransport
from .usb import ProbeError, UsbEndpointDescriptor, rtl_usb_probe


class RTL8192CUDriver(Driver):
    SUPPORTED_CHANNELS = list(range(1, 15))
    CONFLICTING_LINUX_MODULES = ["rtl8192cu", "rtl8xxxu"]
    FAKE_MAC = FakeMacSupport.UNIMPLEMENTED

    def __init__(self, dev: usb.core.Device, id_entry: DeviceID) -> None:
        super().__init__()
        self.dev = dev
        self.id_entry = id_entry
        self.transport = RTL8192CUTransport(dev)
        self.priv = RtlPriv(self.transport)
        self.mac_address: str | None = None
        self.is_warm = False
        self._endpoints: list[UsbEndpointDescriptor] = []
        self._is_high_speed = True
        self._rx_callback: Callable | None = None
        self._disconnect_callback: Callable[[], None] | None = None

    @classmethod
    def from_usb_device(cls, dev: usb.core.Device, id_entry: DeviceID) -> "RTL8192CUDriver":
        return cls(dev, id_entry)

    def register_rx_callback(self, cb: Callable) -> None:
        self._rx_callback = cb

    def register_disconnect_callback(self, cb: Callable[[], None]) -> None:
        self._disconnect_callback = cb

    def _bringup(self) -> None:
        """Everything the cold-boot capture records, from rtl_usb_probe through rtl92cu_hw_init."""
        try:
            rtl_usb_probe(self.priv, self._endpoints, self._is_high_speed)
        except ProbeError as error:
            raise BringUpError("RTL8192CU probe", str(error)) from error
        self.mac_address = self.priv.efuse.dev_addr.hex(":")
        err = rtl92cu_hw_init(self.priv)
        if err:
            raise BringUpError("RTL8192CU hw_init", f"rtl92cu_hw_init failed ({err})")
        raise BringUpError("RTL8192CU start", "rtl_usb_start after hw_init (RX config, monitor) is not ported")

    async def connect(self, progress_cb: ProgressCallback | None = None) -> bool:
        loop = asyncio.get_running_loop()
        progress = progress_cb or (lambda _value, _message: None)
        try:
            progress(0.00, "Claiming USB interface")
            await loop.run_in_executor(None, self.transport.claim)
            assert self.transport.interface is not None
            self._endpoints = [UsbEndpointDescriptor(ep.bEndpointAddress, ep.bmAttributes)
                               for ep in self.transport.interface]
            self._is_high_speed = self.transport.is_high_speed()
            progress(0.05, "Reading EFUSE, downloading firmware, initialising MAC/BB/RF")
            await loop.run_in_executor(None, self._bringup)
        except Exception as error:
            self.transport.release()
            if isinstance(error, BringUpError):
                raise
            raise BringUpError("RTL8192CU bring-up", str(error)) from error
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        raise NotImplementedError("rtl92c_phy_sw_chnl is not ported")

    async def _inject_frame(self, frame_bytes: bytes) -> bool:
        raise NotImplementedError("rtl92cu_tx_fill_desc is not ported")

    def _stamp_tx_seq(self, frame_bytes: bytes) -> bytes:
        return frame_bytes

    async def _enable_rx_acks(self) -> None:
        raise NotImplementedError("RCR setup is not ported")

    async def _disable_rx_acks(self) -> None:
        raise NotImplementedError("RCR setup is not ported")

    async def close(self) -> None:
        self.transport.release()
        self.is_warm = False
