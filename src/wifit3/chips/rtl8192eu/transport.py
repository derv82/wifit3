from __future__ import annotations

import errno

import usb.core
import usb.util

from wifit3.errors import is_device_gone

from . import constants as C
from .core import Rtl8xxxuPriv


class RTL8192EUTransport:
    """usb_control_msg / usb_bulk_msg over pyusb (core.c rtl8xxxu_read8 .. rtl8xxxu_writeN)."""

    def __init__(self, dev: usb.core.Device) -> None:
        self.dev = dev
        self.interface = None
        self.interface_number: int | None = None
        self.removed = False
        self._usb_buf = bytearray(4)   # priv->usb_buf: a failed read returns what is left in it

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

    def rtl8xxxu_parse_usb(self, priv: Rtl8xxxuPriv) -> None:
        """core.c:7567; pipes hold pyusb endpoint addresses rather than endpoint numbers."""
        out_ep: list[int] = []
        for endpoint in self.interface:
            address = endpoint.bEndpointAddress
            xtype = usb.util.endpoint_type(endpoint.bmAttributes)
            direction_in = usb.util.endpoint_direction(address) == usb.util.ENDPOINT_IN
            if direction_in and xtype == usb.util.ENDPOINT_TYPE_BULK:
                if priv.pipe_in:
                    raise ValueError("Too many IN pipes")
                priv.pipe_in = address
            if direction_in and xtype == usb.util.ENDPOINT_TYPE_INTR:
                if priv.pipe_interrupt:
                    raise ValueError("Too many INTERRUPT pipes")
                priv.pipe_interrupt = address
            if not direction_in and xtype == usb.util.ENDPOINT_TYPE_BULK:
                if len(out_ep) >= C.RTL8XXXU_OUT_ENDPOINTS:
                    raise ValueError("Too many OUT pipes")
                out_ep.append(address)
        priv.out_ep = out_ep
        priv.nr_out_eps = len(out_ep)

    def read8(self, address: int) -> int:
        return int.from_bytes(self._read(address, 1), "little")

    def read16(self, address: int) -> int:
        return int.from_bytes(self._read(address, 2), "little")

    def read32(self, address: int) -> int:
        return int.from_bytes(self._read(address, 4), "little")

    def write8(self, address: int, value: int) -> int | None:
        return self._write(address, value.to_bytes(1, "little"))

    def write16(self, address: int, value: int) -> int | None:
        return self._write(address, value.to_bytes(2, "little"))

    def write32(self, address: int, value: int) -> int | None:
        return self._write(address, value.to_bytes(4, "little"))

    def write_block(self, address: int, data: bytes) -> int | None:
        """One rtl8xxxu_writeN block (core.c:836-852): bytes written, or None on a failed transfer."""
        return self._usb_control_msg(C.REALTEK_USB_WRITE, address, bytes(data))

    def bulk_write(self, endpoint: int, data: bytes) -> None:
        """One TX URB: a failed submit or completion drops the frame (core.c:5553-5562, :5113-5138).
        ENODEV marks the device removed and raises."""
        if self.removed:
            raise usb.core.USBError("device removed", errno=errno.ENODEV)
        try:
            self.dev.write(endpoint, data, timeout=C.RTW_USB_CONTROL_MSG_TIMEOUT)
        except usb.core.USBError as e:
            if is_device_gone(e):
                self.removed = True
                raise

    def _read(self, address: int, length: int) -> bytes:
        data = self._usb_control_msg(C.REALTEK_USB_READ, address, length)
        if data is not None:
            self._usb_buf[:len(data)] = bytes(data)
        return bytes(self._usb_buf[:length])

    def _write(self, address: int, data: bytes) -> int | None:
        self._usb_buf[:len(data)] = data
        return self._usb_control_msg(C.REALTEK_USB_WRITE, address, data)

    def _usb_control_msg(self, request_type: int, address: int, data_or_length):
        """usb_control_msg, one try, 500 ms (core.c:611-745). ENODEV marks the device removed and
        raises; any other failure returns None."""
        if self.removed:
            raise usb.core.USBError("device removed", errno=errno.ENODEV)
        try:
            return self.dev.ctrl_transfer(request_type, C.REALTEK_USB_CMD_REQ, address,
                                          C.REALTEK_USB_CMD_IDX, data_or_length,
                                          timeout=C.RTW_USB_CONTROL_MSG_TIMEOUT)
        except usb.core.USBError as e:
            if is_device_gone(e):
                self.removed = True
                raise
            return None
