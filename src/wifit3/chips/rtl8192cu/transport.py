from __future__ import annotations

import usb.core
import usb.util

from . import constants as C


class RTL8192CUTransport:
    """usb.c _usbctrl_vendorreq_sync and the read/write/chunk accessors built on it."""

    def __init__(self, dev: usb.core.Device) -> None:
        self.dev = dev
        self.interface: usb.core.Interface | None = None
        self.interface_number: int | None = None

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
        self._usbctrl_vendorreq_sync(C.REALTEK_USB_VENQT_WRITE, addr, bytes(data))

    def _usb_read_sync(self, addr: int, length: int) -> int:
        data = self._usbctrl_vendorreq_sync(C.REALTEK_USB_VENQT_READ, addr & 0xFFFF, length)
        return int.from_bytes(bytes(data), "little")

    def _usb_write_sync(self, addr: int, value: int, length: int) -> None:
        self._usbctrl_vendorreq_sync(C.REALTEK_USB_VENQT_WRITE, addr & 0xFFFF, value.to_bytes(length, "little"))

    def _usbctrl_vendorreq_sync(self, reqtype: int, value: int, data_or_length: bytes | int):
        """Retries up to MAX_USBCTRL_VENDORREQ_TIMES, except inside the FW window (checksummed)."""
        error: usb.core.USBError | None = None
        for _ in range(C.MAX_USBCTRL_VENDORREQ_TIMES):
            try:
                return self.dev.ctrl_transfer(reqtype, C.REALTEK_USB_VENQT_CMD_REQ, value,
                                              C.REALTEK_USB_VENQT_CMD_IDX, data_or_length,
                                              timeout=C.USB_CONTROL_MSG_TIMEOUT)
            except usb.core.USBError as e:
                error = e
                if C.FW_8192C_START_ADDRESS <= value <= C.FW_8192C_END_ADDRESS:
                    break
        assert error is not None
        raise error
