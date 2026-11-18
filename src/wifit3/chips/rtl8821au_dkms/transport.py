"""RTL8821AU (DKMS) USB transport — Realtek rtw88-family vendor control transfers.

Every register access is a bRequest 0x05 vendor control transfer, tried up to
MAX_USBCTRL_VENDORREQ_TIMES times (usbctrl_vendorreq): read uses
bmRequestType 0xC0, write 0x40, with the 16-bit register address in wValue and
wIndex 0. The firmware-download page writes ride this same control path in
≤196-byte payloads (`writeN`), not bulk-OUT — bulk endpoints are only used for
RX/TX later. [SRC] include/usb_ops.h:19-31, hal/hal_hci/hal_usb.c:338-540.

The read*/write*/writeN surface matches `scripts/rtw88_pcap_replay.ReplayTransport`
so the bring-up code runs unchanged against either real hardware or the pcap.
"""
from __future__ import annotations

import errno
import logging

import usb.core
import usb.util

from wifit3.errors import is_device_gone

from .rx import RXDESC_SIZE
from .constants import (
    MAX_VENDOR_REQ_CMD_SIZE,
    REALTEK_USB_VENQT_CMD_IDX,
    REALTEK_USB_VENQT_CMD_REQ,
    REALTEK_USB_VENQT_READ,
    REALTEK_USB_VENQT_WRITE,
)

logger = logging.getLogger(__name__)

CTRL_TIMEOUT_MS = 500  # [SRC] include/usb_ops_linux.h:22
MAX_USBCTRL_VENDORREQ_TIMES = 10  # [SRC] include/usb_ops_linux.h:29-31, autoconf.h:228-229
MAX_CONTINUAL_IO_ERR = 4          # [SRC] include/rtw_io.h:265-266
FW_START_ADDRESS = 0x1000         # [SRC] include/usb_ops_linux.h:19
# RX bulk-IN read: the buffer must hold a whole USB-aggregated transfer; read
# generously, with a short timeout so the reader thread stays responsive to stop()
# and returns None between bursts of traffic on a quiet channel.
RX_BUF_SIZE = 0x8000        # 32 KB >= the RX-DMA aggregation ceiling
RX_TIMEOUT_MS = 200
_BULK_TIMEOUT_MS = 1000


class RTL8821AUDkmsTransport:
    def __init__(self, dev: usb.core.Device):
        self.dev = dev
        self._in_ep = None  # bulk-IN (RX) endpoint address, probed lazily
        self.continual_io_error = 0
        self.surprise_removed = False

    def usbctrl_vendorreq(self, reqtype: int, addr: int, pdata: bytearray) -> int:
        """[SRC] os_dep/linux/usb_ops_linux.c:26-215: up to 10 tries (one from 0x1000 up), each failure
        logged. ENODEV or 5 failures in a row mark the device removed and raise."""
        if self.surprise_removed:  # RTW_CANNOT_IO, :58-61
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        value = addr & 0xFFFF
        is_read = reqtype == REALTEK_USB_VENQT_READ
        length = len(pdata)
        for vendorreq_times in range(1, MAX_USBCTRL_VENDORREQ_TIMES + 1):
            try:
                result = self.dev.ctrl_transfer(reqtype, REALTEK_USB_VENQT_CMD_REQ, value,
                                                REALTEK_USB_VENQT_CMD_IDX,
                                                length if is_read else pdata, CTRL_TIMEOUT_MS)
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
            if length in (1, 2, 4):
                logger.info("reg 0x%x, usb %s %u fail, status:%d value=0x%x, vendorreq_times:%d",
                            value, "read" if is_read else "write", length, status,
                            int.from_bytes(pdata, "little"), vendorreq_times)
            else:
                logger.info("reg 0x%x, usb %s %u fail, status:%d, vendorreq_times:%d", value,
                            "read" if is_read else "write", length, status, vendorreq_times)
            if is_read and status > 0:
                pdata[:] = bytes(result).ljust(length, b"\x00")
            self.continual_io_error += 1
            if self.continual_io_error > MAX_CONTINUAL_IO_ERR:
                logger.info("[ERROR] continual_io_error:%d > %d", self.continual_io_error,
                            MAX_CONTINUAL_IO_ERR)  # core/rtw_io.c:470
                self.surprise_removed = True
                break
            if value >= FW_START_ADDRESS:
                break
        if self.surprise_removed:
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        return status

    def _read(self, addr: int, length: int) -> bytes:
        pdata = bytearray(length)
        self.usbctrl_vendorreq(REALTEK_USB_VENQT_READ, addr, pdata)
        return bytes(pdata)

    def read8(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 1), "little")

    def read16(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 2), "little")

    def read32(self, addr: int) -> int:
        return int.from_bytes(self._read(addr, 4), "little")

    def writeN(self, addr: int, data: bytes) -> bool:
        data = bytes(data)
        if len(data) > MAX_VENDOR_REQ_CMD_SIZE:
            raise ValueError(f"vendor write {len(data)} > {MAX_VENDOR_REQ_CMD_SIZE} B")
        return self.usbctrl_vendorreq(REALTEK_USB_VENQT_WRITE, addr, bytearray(data)) >= 0  # RTW_STATUS_CODE

    def write8(self, addr: int, val: int) -> bool:
        return self.writeN(addr, (val & 0xFF).to_bytes(1, "little"))

    def write16(self, addr: int, val: int) -> bool:
        return self.writeN(addr, (val & 0xFFFF).to_bytes(2, "little"))

    def write32(self, addr: int, val: int) -> bool:
        return self.writeN(addr, (val & 0xFFFFFFFF).to_bytes(4, "little"))

    # --- bulk OUT (firmware uses control writes; bulk-OUT is TX, ep 0x09) ---
    def bulk_out(self, data: bytes) -> None:
        """usb_write_port: a failed write drops the frame. [SRC] os_dep/linux/usb_ops_linux.c:567-700."""
        if self.surprise_removed:  # RTW_CANNOT_TX, :581-589
            raise usb.core.USBError("surprise removed", errno=errno.ENODEV)
        try:
            self.dev.write(0x09, data, _BULK_TIMEOUT_MS)
        except usb.core.USBError as e:
            logger.info("usb_write_port, status=%d", -(e.errno or errno.EIO))  # :673
            if is_device_gone(e):
                self.surprise_removed = True
                raise

    # --- bulk IN (RX, ep 0x84) --------------------------------------------
    def _bulk_in_ep(self) -> int:
        """Probe the active interface for the bulk-IN (RX) endpoint, cached."""
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
        raise RuntimeError("RTL8821AU: no bulk-IN endpoint on the active interface")

    def bulk_in(self, size: int = RX_BUF_SIZE, timeout: int = RX_TIMEOUT_MS):
        """One blocking bulk-IN read. Returns the raw buffer, or None on a benign
        timeout (no traffic this interval). Raises usb.core.USBError on a real fault.

        A read timeout is benign and common (every quiet channel yields one). pyusb
        raises USBTimeoutError for it; catch that type rather than sniffing the message,
        because the Windows/WinUSB backend reports errno 10060 "Operation timed out"
        (no "timeout" substring), so a message check silently misses it and the reader
        counts benign timeouts as fatal.
        """
        if self.surprise_removed:  # RTW_CANNOT_RX. usb_ops_linux.c:1012
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
        if len(data) >= RXDESC_SIZE:  # usb_read_port_complete. usb_ops_linux.c:939-945
            self.continual_io_error = 0
        return data

    def close(self) -> None:
        usb.util.dispose_resources(self.dev)
