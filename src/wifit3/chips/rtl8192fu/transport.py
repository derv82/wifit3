from __future__ import annotations

import usb.core
import usb.util

from . import constants as C
from .core import Rtl8xxxuPriv


class RTL8192FUTransport:
    """usb_control_msg / usb_bulk_msg over pyusb (core.c rtl8xxxu_read8 .. rtl8xxxu_writeN)."""

    def __init__(self, dev: usb.core.Device) -> None:
        self.dev = dev
        self.interface = None
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

    def write8(self, address: int, value: int) -> None:
        self._write(address, value.to_bytes(1, "little"))

    def write16(self, address: int, value: int) -> None:
        self._write(address, value.to_bytes(2, "little"))

    def write32(self, address: int, value: int) -> None:
        self._write(address, value.to_bytes(4, "little"))

    def write_block(self, address: int, data: bytes) -> None:
        self._write(address, bytes(data))

    def bulk_write(self, endpoint: int, data: bytes) -> None:
        written = self.dev.write(endpoint, data, timeout=C.RTW_USB_CONTROL_MSG_TIMEOUT)
        if written != len(data):
            raise IOError(f"short bulk write to {endpoint:#04x}: {written}/{len(data)}")

    def _read(self, address: int, length: int) -> bytes:
        data = bytes(self.dev.ctrl_transfer(
            C.REALTEK_USB_READ, C.REALTEK_USB_CMD_REQ, address, C.REALTEK_USB_CMD_IDX, length,
            timeout=C.RTW_USB_CONTROL_MSG_TIMEOUT,
        ))
        if len(data) != length:
            raise IOError(f"short register read at {address:#06x}: {len(data)}/{length}")
        return data

    def _write(self, address: int, data: bytes) -> None:
        written = self.dev.ctrl_transfer(
            C.REALTEK_USB_WRITE, C.REALTEK_USB_CMD_REQ, address, C.REALTEK_USB_CMD_IDX, data,
            timeout=C.RTW_USB_CONTROL_MSG_TIMEOUT,
        )
        if written != len(data):
            raise IOError(f"short register write at {address:#06x}: {written}/{len(data)}")
