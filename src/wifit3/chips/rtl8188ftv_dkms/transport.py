"""RTL8188FTV DKMS USB transport — vendor-control register reads/writes.

Ported from ``usb_read8/16/32`` + ``usb_write8/16/32/N``
(hal/hal_hci/hal_usb.c:312-499): every access is a control transfer, tried up to
MAX_USBCTRL_VENDORREQ_TIMES times (usbctrl_vendorreq), with
``bRequest 0x05`` (include/usb_ops.h:24-27), ``wValue = addr & 0xffff``,
``wIndex 0``. Read replies are little-endian.
"""
from __future__ import annotations

import errno
import logging

import usb.core

from wifit3.errors import is_device_gone

from .rx import RXDESC_SIZE
from .constants import (
    REALTEK_USB_VENQT_CMD_REQ,
    REALTEK_USB_VENQT_READ,
    REALTEK_USB_VENQT_WRITE,
    RTW_USB_CONTROL_MSG_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)

MAX_USBCTRL_VENDORREQ_TIMES = 10  # [SRC] include/usb_ops_linux.h:34-38, autoconf.h:182-183
MAX_CONTINUAL_IO_ERR = 4          # [SRC] include/rtw_io.h:314-315
FW_START_ADDRESS = 0x1000         # [SRC] include/usb_ops_linux.h:24
_BULK_OUT_EP = 0x02
_BULK_TIMEOUT_MS = 2000


class Rtl8188ftvDkmsTransport:
    """Vendor control-transfer transport for the RTL8188FTV DKMS port."""

    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self.continual_io_error = 0
        self.surprise_removed = False

    def usbctrl_vendorreq(self, reqtype: int, addr: int, pdata: bytearray) -> int:
        """[SRC] os_dep/linux/usb_ops_linux.c:30-150: up to 10 tries (one from 0x1000 up). ENODEV or
        5 failures in a row mark the device removed and raise. The C's failure print is DBG_8192C,
        compiled out without CONFIG_DEBUG (autoconf.h:305): debug here."""
        if self.surprise_removed:  # RTW_CANNOT_IO, :61
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        value = addr & 0xFFFF
        is_read = reqtype == REALTEK_USB_VENQT_READ
        length = len(pdata)
        for vendorreq_times in range(1, MAX_USBCTRL_VENDORREQ_TIMES + 1):
            try:
                result = self.dev.ctrl_transfer(reqtype, REALTEK_USB_VENQT_CMD_REQ, value, 0,
                                                length if is_read else pdata,
                                                RTW_USB_CONTROL_MSG_TIMEOUT_MS)
            except usb.core.USBError as e:
                status = -(e.errno or errno.EIO)
                if is_device_gone(e):
                    self.surprise_removed = True
            else:
                status = len(result) if is_read else result
                if status == length:
                    if is_read:
                        pdata[:] = bytes(result)
                    self.continual_io_error = 0
                    return status
            logger.debug("reg 0x%x, usb %s %u fail, status:%d value=0x%x, vendorreq_times:%d",
                         value, "read" if is_read else "write", length, status,
                         int.from_bytes(pdata[:4], "little"), vendorreq_times)  # :128-129
            if is_read and status > 0:
                pdata[:] = bytes(result).ljust(length, bytes(1))
            self.continual_io_error += 1
            if self.continual_io_error > MAX_CONTINUAL_IO_ERR:
                logger.debug("[ERROR] continual_io_error:%d > %d", self.continual_io_error,
                             MAX_CONTINUAL_IO_ERR)  # core/rtw_io.c:509, DBG_871X compiled out
                self.surprise_removed = True
                break
            if value >= FW_START_ADDRESS:
                break
        if self.surprise_removed:
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        return status

    def read(self, addr: int, width: int) -> int:
        pdata = bytearray(width)
        self.usbctrl_vendorreq(REALTEK_USB_VENQT_READ, addr, pdata)
        return int.from_bytes(pdata, "little")

    def write(self, addr: int, width: int, value: int) -> bool:
        pdata = bytearray(value.to_bytes(width, "little"))
        return self.usbctrl_vendorreq(REALTEK_USB_VENQT_WRITE, addr, pdata) >= 0  # RTW_STATUS_CODE

    def read8(self, addr: int) -> int:
        return self.read(addr, 1)

    def read16(self, addr: int) -> int:
        return self.read(addr, 2)

    def read32(self, addr: int) -> int:
        return self.read(addr, 4)

    def write8(self, addr: int, value: int) -> bool:
        return self.write(addr, 1, value)

    def write16(self, addr: int, value: int) -> bool:
        return self.write(addr, 2, value)

    def write32(self, addr: int, value: int) -> bool:
        return self.write(addr, 4, value)

    def writeN(self, addr: int, data: bytes) -> bool:
        return self.usbctrl_vendorreq(REALTEK_USB_VENQT_WRITE, addr, bytearray(data)) >= 0  # RTW_STATUS_CODE

    def bulk_in(self, ep: int, max_size: int = 16384,
                timeout_ms: int = 100) -> bytes | None:
        if self.surprise_removed:  # RTW_CANNOT_RX. usb_ops_linux.c:1047
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        try:
            data = bytes(self.dev.read(ep, max_size, timeout_ms))
        except usb.core.USBError as e:
            err = getattr(e, "errno", None)
            if (isinstance(e, usb.core.USBTimeoutError) or err in (110, 10060)
                    or "timeout" in str(e).lower()):
                return None
            raise
        if len(data) >= RXDESC_SIZE:  # usb_read_port_complete. usb_ops_linux.c:968
            self.continual_io_error = 0
        return data

    def bulk_out(self, data: bytes) -> None:
        """usb_write_port: a failed write drops the frame. [SRC] os_dep/linux/usb_ops_linux.c:549-694."""
        if self.surprise_removed:  # RTW_CANNOT_TX, :569
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        try:
            self.dev.write(_BULK_OUT_EP, bytes(data), _BULK_TIMEOUT_MS)
        except usb.core.USBError as e:
            if is_device_gone(e):
                self.surprise_removed = True
                raise
            logger.debug("usb_write_port, status=%d", -(e.errno or errno.EIO))  # :662, DBG_871X compiled out
