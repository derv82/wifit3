"""RTL8822CU USB transport and endpoint inspection."""
from __future__ import annotations

import errno
import logging
from dataclasses import dataclass

import usb.core

from wifit3.chips.rtw88_base.transport import (
    USB_CMD_REQ,
    USB_REQTYPE_READ,
    USB_REQTYPE_WRITE,
    USB_VENQT_CMD_IDX,
    Rtw88Transport,
)
from wifit3.errors import is_device_gone

from .constants import (
    USB_ENDPOINT_DIR_MASK,
    USB_ENDPOINT_TYPE_BULK,
    USB_ENDPOINT_TYPE_MASK,
    USB_INTERFACE_CLASS_VENDOR,
)


@dataclass(frozen=True)
class EndpointLayout:
    interface: int
    bulk_in: tuple[int, ...]
    bulk_out: tuple[int, ...]


MAX_USBCTRL_VENDORREQ_TIMES = 10   # [SRC include/usb_ops_linux.h:29-31]
MAX_CONTINUAL_IO_ERR = 4           # [SRC include/rtw_io.h:265-266]
FW_START_ADDRESS = 0x1000          # [SRC include/usb_ops_linux.h:19]: no retry from here up

logger = logging.getLogger(__name__)


# ON-section page-switch echo, guarded #if CONFIG_RTL8822B||8821C||8822C||8822E
# [SRC os_dep/linux/usb_ops_linux.c:172-203]: every vendor access to an ON-section
# register (addr < 0xFE00 and (addr <= 0xFF or 0x1000..0x10FF)) is followed by a
# 1-byte write to 0x4E0 carrying IO-buffer byte[0] (read-back byte for a read, the
# written byte for a write).
def _is_on_section(addr: int) -> bool:
    return addr < 0xFE00 and (addr <= 0xFF or 0x1000 <= addr <= 0x10FF)


class RTL8822CUTransport(Rtw88Transport):
    """RTL8822C uses the common rtw88 vendor-control protocol, plus the ON-section
    0x4E0 echo the 8822b/8821c/8822c/8822e USB path appends to each such access."""

    def __init__(self, dev: usb.core.Device, timeout_ms: int = 500):
        super().__init__(dev, timeout_ms)
        self.continual_io_error = 0
        self.surprise_removed = False

    def usbctrl_vendorreq(self, reqtype: int, value: int, data_or_length) -> bytes:
        """[SRC os_dep/linux/usb_ops_linux.c:26-205]: up to 10 tries (one from 0x1000 up), then the
        0x4E0 echo. A failure is logged; a failed read returns its partial bytes, else zeros. ENODEV
        or 5 failures in a row mark the device removed and raise."""
        if self.surprise_removed:  # RTW_CANNOT_IO, :59-62
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        is_read = isinstance(data_or_length, int)
        length = data_or_length if is_read else len(data_or_length)
        pdata = bytearray(length) if is_read else bytearray(data_or_length)
        for vendorreq_times in range(1, MAX_USBCTRL_VENDORREQ_TIMES + 1):
            io_buf = bytearray(length) if is_read else bytearray(data_or_length)
            try:
                result = self.dev.ctrl_transfer(reqtype, USB_CMD_REQ, value, USB_VENQT_CMD_IDX,
                                                data_or_length, self.timeout_ms)
            except usb.core.USBError as e:
                status = -(e.errno or errno.EIO)
                if is_device_gone(e):
                    self.surprise_removed = True
            else:
                status = len(result) if is_read else result
                if is_read:
                    io_buf[:len(result)] = bytes(result)
                if status == length:
                    if is_read:
                        pdata[:] = io_buf
                    self.continual_io_error = 0
                    break
            if length in (1, 2, 4):
                logger.info("reg 0x%x, usb %s %u fail, status:%d value=0x%x, vendorreq_times:%d",
                            value, "read" if is_read else "write", length, status,
                            int.from_bytes(pdata, "little"), vendorreq_times)
            else:
                logger.info("reg 0x%x, usb %s %u fail, status:%d, vendorreq_times:%d", value,
                            "read" if is_read else "write", length, status, vendorreq_times)
            if is_read and status > 0:
                pdata[:] = io_buf
            self.continual_io_error += 1
            if self.continual_io_error > MAX_CONTINUAL_IO_ERR:
                logger.info("[ERROR] continual_io_error:%d > %d", self.continual_io_error,
                            MAX_CONTINUAL_IO_ERR)  # core/rtw_io.c:470
                self.surprise_removed = True
            if self.surprise_removed or value >= FW_START_ADDRESS:
                break
        if _is_on_section(value):
            self._echo(io_buf[0] if io_buf else 0)
        if self.surprise_removed:
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        return bytes(pdata)

    def _echo(self, low_byte: int) -> None:
        """The write to 0x4E0 after an ON-section access; only its own failure is logged
        [SRC os_dep/linux/usb_ops_linux.c:172-203]."""
        t_reg = 0x4E0  # :193
        try:
            status = self.dev.ctrl_transfer(USB_REQTYPE_WRITE, USB_CMD_REQ, t_reg,
                                            USB_VENQT_CMD_IDX, bytes([low_byte & 0xFF]),
                                            self.timeout_ms)
        except usb.core.USBError as e:
            status = -(e.errno or errno.EIO)
        if status == 1:
            self.continual_io_error = 0
        else:
            logger.info("reg 0x%x, usb write 1 fail, status:%d", t_reg, status)

    def _read(self, addr: int, length: int) -> bytes:
        return self.usbctrl_vendorreq(USB_REQTYPE_READ, addr, length)

    def _write(self, addr: int, payload) -> None:
        self.usbctrl_vendorreq(USB_REQTYPE_WRITE, addr, bytes(payload))

    def endpoints(self) -> EndpointLayout:
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            if intf.bInterfaceClass != USB_INTERFACE_CLASS_VENDOR:
                continue
            ins: list[int] = []
            outs: list[int] = []
            for ep in intf:
                if (ep.bmAttributes & USB_ENDPOINT_TYPE_MASK) != USB_ENDPOINT_TYPE_BULK:
                    continue
                if ep.bEndpointAddress & USB_ENDPOINT_DIR_MASK:
                    ins.append(ep.bEndpointAddress)
                else:
                    outs.append(ep.bEndpointAddress)
            if ins or outs:
                return EndpointLayout(intf.bInterfaceNumber, tuple(ins), tuple(outs))
        raise RuntimeError("RTL8822CU: no vendor-specific bulk interface")
