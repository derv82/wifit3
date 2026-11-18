from __future__ import annotations

import errno
import logging

import usb.core
import usb.util

from wifit3.errors import is_device_gone

from . import constants as C

logger = logging.getLogger(__name__)


class RTL8192CUTransport:
    """usb.c _usbctrl_vendorreq_sync and the read/write/chunk accessors built on it."""

    def __init__(self, dev: usb.core.Device) -> None:
        self.dev = dev
        self.interface: usb.core.Interface | None = None
        self.interface_number: int | None = None
        self.removed = False
        self.usb_data = [bytearray(4) for _ in range(C.RTL_USB_MAX_RX_COUNT)]   # rtlpriv->usb_data
        self.usb_data_index = 0
        self.count = 0   # usb.c:34 is a module-wide static; one counter per device here

    def claim(self) -> None:
        interface = self.dev.get_active_configuration()[(0, 0)]
        try:
            if self.dev.is_kernel_driver_active(interface.bInterfaceNumber):
                self.dev.detach_kernel_driver(interface.bInterfaceNumber)
        except (NotImplementedError, usb.core.USBError):
            pass
        usb.util.claim_interface(self.dev, interface.bInterfaceNumber)
        self.interface = interface
        self.interface_number = interface.bInterfaceNumber

    def release(self) -> None:
        if self.interface_number is None:
            return
        try:
            usb.util.release_interface(self.dev, self.interface_number)
        finally:
            self.interface = None
            self.interface_number = None

    def is_high_speed(self) -> bool:
        return self.dev.speed == usb.util.SPEED_HIGH

    def bulk_read(self, endpoint: int, size: int, timeout_ms: int) -> bytes | None:
        """One bulk-IN URB of ``size`` bytes; None when nothing arrived within ``timeout_ms``."""
        try:
            return bytes(self.dev.read(endpoint, size, timeout_ms))
        except usb.core.USBTimeoutError:
            return None

    def bulk_write(self, endpoint: int, data: bytes, max_packet_size: int, timeout_ms: int) -> None:
        """One bulk-OUT URB with URB_ZERO_PACKET (usb.c:840): PyUSB sends no ZLP itself. A failed URB
        drops the frame (usb.c:800-802, :820-824); ENODEV marks the device removed and raises."""
        if self.removed:
            raise usb.core.USBError("device removed", errno=errno.ENODEV)
        try:
            self.dev.write(endpoint, data, timeout=timeout_ms)
            if len(data) % max_packet_size == 0:
                self.dev.write(endpoint, b"", timeout=timeout_ms)
        except usb.core.USBError as e:
            if is_device_gone(e):
                self.removed = True
                raise
            logger.error("Urb has error status 0x%X", -(e.errno or errno.EIO) & 0xFFFFFFFF)

    def read8(self, addr: int) -> int:
        return self._usb_read_sync(addr, 1)

    def read16(self, addr: int) -> int:
        return self._usb_read_sync(addr, 2)

    def read32(self, addr: int) -> int:
        return self._usb_read_sync(addr, 4)

    def write8(self, addr: int, value: int) -> None:
        self._usb_write_sync(addr, value & 0xFF, 1)

    def write16(self, addr: int, value: int) -> None:
        self._usb_write_sync(addr, value & 0xFFFF, 2)

    def write32(self, addr: int, value: int) -> None:
        self._usb_write_sync(addr, value & 0xFFFFFFFF, 4)

    def write_chunk(self, addr: int, data: bytes) -> None:
        self._usbctrl_vendorreq_sync(C.REALTEK_USB_VENQT_WRITE, addr, bytearray(data), len(data))

    def _usb_read_sync(self, addr: int, length: int) -> int:
        data = self._next_usb_data()
        self._usbctrl_vendorreq_sync(C.REALTEK_USB_VENQT_READ, addr & 0xFFFF, data, length)
        return int.from_bytes(data[:length], "little")

    def _usb_write_sync(self, addr: int, value: int, length: int) -> None:
        data = self._next_usb_data()
        data[:] = value.to_bytes(4, "little")
        self._usbctrl_vendorreq_sync(C.REALTEK_USB_VENQT_WRITE, addr & 0xFFFF, data, length)

    def _next_usb_data(self) -> bytearray:
        """usb.c:68-71, :85-88: the next rtlpriv->usb_data slot; a failed read leaves its old bytes."""
        self.usb_data_index += 1
        if self.usb_data_index >= C.RTL_USB_MAX_RX_COUNT:
            self.usb_data_index = 0
        return self.usb_data[self.usb_data_index]

    def _usbctrl_vendorreq_sync(self, reqtype: int, value: int, pdata: bytearray, length: int) -> None:
        """usb.c:28-58: up to 10 tries, one inside the FW window (checksummed); a failure is logged (the
        first 4 only) and dropped. ENODEV marks the device removed and raises."""
        if self.removed:
            raise usb.core.USBError("device removed", errno=errno.ENODEV)
        data_or_length = length if reqtype == C.REALTEK_USB_VENQT_READ else bytes(pdata[:length])
        status = 0
        for _ in range(C.MAX_USBCTRL_VENDORREQ_TIMES):
            try:
                received = self.dev.ctrl_transfer(reqtype, C.REALTEK_USB_VENQT_CMD_REQ, value,
                                                  C.REALTEK_USB_VENQT_CMD_IDX, data_or_length,
                                                  timeout=C.USB_CONTROL_MSG_TIMEOUT)
                if reqtype == C.REALTEK_USB_VENQT_READ:
                    pdata[:len(received)] = bytes(received)
                return
            except usb.core.USBError as e:
                if is_device_gone(e):
                    self.removed = True
                    raise
                status = -(e.errno or errno.EIO)
                if C.FW_8192C_START_ADDRESS <= value <= C.FW_8192C_END_ADDRESS:
                    break
        if self.count < 4:
            logger.error("reg 0x%x, usbctrl_vendorreq TimeOut! status:0x%x value=0x%x reqtype=0x%x",
                         value, status & 0xFFFFFFFF, int.from_bytes(pdata[:4], "little"), reqtype)
        self.count += 1
