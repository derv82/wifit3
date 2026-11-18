"""RTL8188EUS raw USB transport — Realtek vendor control + bulk I/O.

Every register access is a vendor request (0x05), tried up to MAX_USBCTRL_VENDORREQ_TIMES
times: wValue = 16-bit register
offset, wIndex = 0, data stage 1/2/4 bytes little-endian [SRC] os_dep/linux/
usb_ops_linux.c usbctrl_vendorreq. Unlike the Jaguar-series cards, the 8188e
uploads firmware over EP0 too: ``write_block`` is a wide (<=196 B) vendor control
write to the FW SRAM window (rtw_writeN) [SRC] rtl8188e_hal_init.c _BlockWrite.

The bring-up code is transport-agnostic (duck-typed read8/16/32, write8/16/32,
write_block, bulk_in/out), so the same routines drive this PyUSB transport on
hardware and the pcap-replay transport in the verifiers.
"""
from __future__ import annotations

import errno
import logging
import struct

import usb.core
import usb.util

from wifit3.errors import is_device_gone

from .rx import RXDESC_SIZE
from .constants import REALTEK_VENDOR_REQUEST, REQ_TYPE_READ, REQ_TYPE_WRITE

logger = logging.getLogger(__name__)

_CTRL_TIMEOUT_MS = 500  # [SRC] include/usb_ops_linux.h:22
MAX_USBCTRL_VENDORREQ_TIMES = 10  # [SRC] include/usb_ops_linux.h:29-31, autoconf.h:224-225
MAX_CONTINUAL_IO_ERR = 4          # [SRC] include/rtw_io.h:308-309
FW_START_ADDRESS = 0x1000         # [SRC] include/usb_ops_linux.h:19
_BULK_TIMEOUT_MS = 1000
# RX bulk-IN read: 16 KB holds a whole USB-aggregated transfer for this chip; treat a
# timeout (quiet channel) as benign.
RX_BUF_SIZE = 0x4000
RX_TIMEOUT_MS = 200


class Rtl8188eusTransport:
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self._in_ep = None   # bulk-IN endpoint, probed lazily
        self._out_ep = None  # bulk-OUT endpoint, probed lazily
        self.continual_io_error = 0
        self.surprise_removed = False

    # --- register access ---------------------------------------------------
    def usbctrl_vendorreq(self, reqtype: int, addr: int, pdata: bytearray) -> int:
        """[SRC] os_dep/linux/usb_ops_linux.c:26-150: up to 10 tries (one from 0x1000 up), each failure
        logged. ENODEV or 5 failures in a row mark the device removed and raise."""
        if self.surprise_removed:  # RTW_CANNOT_IO, :55-58
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        value = addr & 0xFFFF
        is_read = reqtype == REQ_TYPE_READ
        length = len(pdata)
        for vendorreq_times in range(1, MAX_USBCTRL_VENDORREQ_TIMES + 1):
            try:
                result = self.dev.ctrl_transfer(reqtype, REALTEK_VENDOR_REQUEST, value, 0,
                                                length if is_read else pdata, _CTRL_TIMEOUT_MS)
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
            # RTW_INFO is compiled out: CONFIG_RTW_DEBUG = n (Makefile:91, rtw_debug.h:34)
            logger.debug("reg 0x%x, usb %s %u fail, status:%d value=0x%x, vendorreq_times:%d",
                         value, "read" if is_read else "write", length, status,
                         int.from_bytes(pdata[:4], "little"), vendorreq_times)  # :115-116
            if is_read and status > 0:
                pdata[:] = bytes(result).ljust(length, b"\x00")
            self.continual_io_error += 1
            if self.continual_io_error > MAX_CONTINUAL_IO_ERR:
                logger.debug("[ERROR] continual_io_error:%d > %d", self.continual_io_error,
                             MAX_CONTINUAL_IO_ERR)  # core/rtw_io.c:470, RTW_INFO compiled out
                self.surprise_removed = True
                break
            if value >= FW_START_ADDRESS:
                break
        if self.surprise_removed:
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        return status

    def _read(self, addr: int, length: int) -> bytes:
        pdata = bytearray(length)
        self.usbctrl_vendorreq(REQ_TYPE_READ, addr, pdata)
        return bytes(pdata)

    def _write(self, addr: int, data: bytes) -> bool:
        return self.usbctrl_vendorreq(REQ_TYPE_WRITE, addr, bytearray(data)) >= 0  # RTW_STATUS_CODE

    def read8(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 1), "little")

    def read16(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 2), "little")

    def read32(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 4), "little")

    def write8(self, addr: int, value: int) -> bool:
        return self._write(addr, bytes([value & 0xFF]))

    def write16(self, addr: int, value: int) -> bool:
        return self._write(addr, struct.pack("<H", value & 0xFFFF))

    def write32(self, addr: int, value: int) -> bool:
        return self._write(addr, struct.pack("<I", value & 0xFFFFFFFF))

    def write_block(self, addr: int, data: bytes) -> bool:
        """Wide vendor control write (firmware-page chunks, <=196 B)."""
        return self._write(addr, bytes(data))

    # --- bulk OUT (TX) -----------------------------------------------------
    def _bulk_out_ep(self) -> int:
        if self._out_ep is not None:
            return self._out_ep
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            for ep in intf:
                if (usb.util.endpoint_direction(ep.bEndpointAddress)
                        == usb.util.ENDPOINT_OUT
                        and usb.util.endpoint_type(ep.bmAttributes)
                        == usb.util.ENDPOINT_TYPE_BULK):
                    self._out_ep = ep.bEndpointAddress
                    return self._out_ep
        raise RuntimeError("RTL8188EUS: no bulk-OUT endpoint on the active interface")

    def bulk_out(self, data: bytes) -> None:
        """usb_write_port: a failed write drops the frame. [SRC] os_dep/linux/usb_ops_linux.c:543-682."""
        if self.surprise_removed:  # RTW_CANNOT_TX, :561
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        try:
            self.dev.write(self._bulk_out_ep(), data, _BULK_TIMEOUT_MS)
        except usb.core.USBError as e:
            if is_device_gone(e):
                self.surprise_removed = True
                raise
            logger.debug("usb_write_port, status=%d", -(e.errno or errno.EIO))  # :653, RTW_INFO compiled out

    # --- bulk IN (RX) ------------------------------------------------------
    def _bulk_in_ep(self) -> int:
        if self._in_ep is not None:
            return self._in_ep
        cfg = self.dev.get_active_configuration()
        for intf in cfg:
            for ep in intf:
                if (usb.util.endpoint_direction(ep.bEndpointAddress)
                        == usb.util.ENDPOINT_IN
                        and usb.util.endpoint_type(ep.bmAttributes)
                        == usb.util.ENDPOINT_TYPE_BULK):
                    self._in_ep = ep.bEndpointAddress
                    return self._in_ep
        raise RuntimeError("RTL8188EUS: no bulk-IN endpoint on the active interface")

    def bulk_in(self, size: int = RX_BUF_SIZE, timeout: int = RX_TIMEOUT_MS):
        """One blocking bulk-IN read; None on a benign timeout (quiet channel)."""
        if self.surprise_removed:  # RTW_CANNOT_RX. usb_ops_linux.c:993
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        try:
            data = bytes(self.dev.read(self._bulk_in_ep(), size, timeout))
        except usb.core.USBTimeoutError:
            return None
        except usb.core.USBError as e:
            if (getattr(e, "errno", None) in (110, 10060)
                    or "timed out" in str(e).lower() or "timeout" in str(e).lower()):
                return None
            raise
        if len(data) >= RXDESC_SIZE:  # usb_read_port_complete. usb_ops_linux.c:925
            self.continual_io_error = 0
        return data

    def close(self) -> None:
        usb.util.dispose_resources(self.dev)
