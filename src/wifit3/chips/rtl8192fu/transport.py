from __future__ import annotations

from dataclasses import dataclass

import usb.core
import usb.util

from . import constants as C


@dataclass(frozen=True)
class USBEndpoints:
    bulk_in: int
    interrupt_in: int | None
    bulk_out: tuple[int, ...]


class RTL8192FUTransport:
    def __init__(self, dev: usb.core.Device) -> None:
        self.dev = dev
        self.interface_number: int | None = None
        self.endpoints: USBEndpoints | None = None

    def claim(self) -> None:
        cfg = self.dev.get_active_configuration()
        interface = cfg[(0, 0)]
        interface_number = interface.bInterfaceNumber
        try:
            if self.dev.is_kernel_driver_active(interface_number):
                self.dev.detach_kernel_driver(interface_number)
        except (NotImplementedError, usb.core.USBError):
            pass
        usb.util.claim_interface(self.dev, interface_number)
        self.interface_number = interface_number
        self.endpoints = self.parse_usb(interface)

    def release(self) -> None:
        if self.interface_number is None:
            return
        try:
            usb.util.release_interface(self.dev, self.interface_number)
        finally:
            self.interface_number = None
            self.endpoints = None

    @staticmethod
    def parse_usb(interface) -> USBEndpoints:
        bulk_in: int | None = None
        interrupt_in: int | None = None
        bulk_out: list[int] = []

        for endpoint in interface:
            address = endpoint.bEndpointAddress
            transfer_type = usb.util.endpoint_type(endpoint.bmAttributes)
            direction = usb.util.endpoint_direction(address)
            if direction == usb.util.ENDPOINT_IN and transfer_type == usb.util.ENDPOINT_TYPE_BULK:
                if bulk_in is not None:
                    raise ValueError("too many bulk-IN endpoints")
                bulk_in = address
            elif direction == usb.util.ENDPOINT_IN and transfer_type == usb.util.ENDPOINT_TYPE_INTR:
                if interrupt_in is not None:
                    raise ValueError("too many interrupt-IN endpoints")
                interrupt_in = address
            elif direction == usb.util.ENDPOINT_OUT and transfer_type == usb.util.ENDPOINT_TYPE_BULK:
                if len(bulk_out) >= C.RTL8XXXU_OUT_ENDPOINTS:
                    raise ValueError("too many bulk-OUT endpoints")
                bulk_out.append(address)

        if bulk_in is None:
            raise ValueError("missing bulk-IN endpoint")
        if not 1 <= len(bulk_out) <= C.RTL8XXXU_OUT_ENDPOINTS:
            raise ValueError(f"unsupported USB TX endpoint count: {len(bulk_out)}")
        return USBEndpoints(bulk_in, interrupt_in, tuple(bulk_out))

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
        self._write(address, data)

    def bulk_write(self, endpoint: int, data: bytes, timeout: int = 500) -> None:
        written = self.dev.write(endpoint, data, timeout=timeout)
        if written != len(data):
            raise IOError(f"short bulk write to {endpoint:#04x}: {written}/{len(data)}")

    def _read(self, address: int, length: int) -> bytes:
        data = self.dev.ctrl_transfer(
            C.REALTEK_USB_READ,
            C.REALTEK_USB_CMD_REQ,
            address,
            C.REALTEK_USB_CMD_IDX,
            length,
            timeout=C.RTW_USB_CONTROL_MSG_TIMEOUT,
        )
        result = bytes(data)
        if len(result) != length:
            raise IOError(f"short register read at {address:#06x}: {len(result)}/{length}")
        return result

    def _write(self, address: int, data: bytes) -> None:
        written = self.dev.ctrl_transfer(
            C.REALTEK_USB_WRITE,
            C.REALTEK_USB_CMD_REQ,
            address,
            C.REALTEK_USB_CMD_IDX,
            data,
            timeout=C.RTW_USB_CONTROL_MSG_TIMEOUT,
        )
        if written != len(data):
            raise IOError(f"short register write at {address:#06x}: {written}/{len(data)}")
