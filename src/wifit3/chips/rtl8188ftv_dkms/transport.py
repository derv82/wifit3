"""RTL8188FTV DKMS USB transport — vendor-control register reads/writes.

Ported from ``usb_read8/16/32`` + ``usb_write8/16/32/N``
(hal/hal_hci/hal_usb.c:312-499): every access is one control transfer with
``bRequest 0x05`` (include/usb_ops.h:24-27), ``wValue = addr & 0xffff``,
``wIndex 0``. Read replies are little-endian.
"""
from __future__ import annotations

import usb.core

from .constants import (
    REALTEK_USB_VENQT_CMD_REQ,
    REALTEK_USB_VENQT_READ,
    REALTEK_USB_VENQT_WRITE,
    RTW_USB_CONTROL_MSG_TIMEOUT_MS,
)


class Rtl8188ftvDkmsTransport:
    """Vendor control-transfer transport for the RTL8188FTV DKMS port."""

    def __init__(self, dev: usb.core.Device):
        self.dev = dev

    def _ctrl(self, read: bool, addr: int, data: bytes | None, length: int) -> bytes:
        reqtype = REALTEK_USB_VENQT_READ if read else REALTEK_USB_VENQT_WRITE
        return bytes(self.dev.ctrl_transfer(
            reqtype, REALTEK_USB_VENQT_CMD_REQ, addr & 0xFFFF, 0,
            data if data is not None else length,
            RTW_USB_CONTROL_MSG_TIMEOUT_MS))

    def read(self, addr: int, width: int) -> int:
        return int.from_bytes(self._ctrl(True, addr, None, width), "little")

    def write(self, addr: int, width: int, value: int) -> None:
        self._ctrl(False, addr, value.to_bytes(width, "little"), width)

    def read8(self, addr: int) -> int:
        return self.read(addr, 1)

    def read16(self, addr: int) -> int:
        return self.read(addr, 2)

    def read32(self, addr: int) -> int:
        return self.read(addr, 4)

    def write8(self, addr: int, value: int) -> None:
        self.write(addr, 1, value)

    def write16(self, addr: int, value: int) -> None:
        self.write(addr, 2, value)

    def write32(self, addr: int, value: int) -> None:
        self.write(addr, 4, value)

    def writeN(self, addr: int, data: bytes) -> None:
        self._ctrl(False, addr, bytes(data), len(data))

    def bulk_in(self, ep: int, max_size: int = 16384,
                timeout_ms: int = 100) -> bytes | None:
        try:
            return bytes(self.dev.read(ep, max_size, timeout_ms))
        except usb.core.USBError as e:
            err = getattr(e, "errno", None)
            if err in (110, 10060) or "timeout" in str(e).lower():
                return None
            raise
