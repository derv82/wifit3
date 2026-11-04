"""A pyusb Device stand-in whose transfers are claimed from a CaptureWalk; libusb is never loaded."""
from __future__ import annotations

import errno
import struct
from array import array
from dataclasses import dataclass, field
from typing import Iterator

import usb.core

from tests.verify_pcap.capture_walk import CaptureWalk
from tests.verify_pcap.usb_capture import TransferType, UsbTransfer
from tests.verify_pcap.virtual_clock_loop import current_producer_name

DESCRIPTOR_TYPE_INTERFACE = 4
DESCRIPTOR_TYPE_ENDPOINT = 5
# Endpoint descriptor bmAttributes order differs from usbmon's transfer-type order.
TRANSFER_TYPE_BY_ENDPOINT_ATTRIBUTES = (TransferType.CONTROL, TransferType.ISOCHRONOUS,
                                        TransferType.BULK, TransferType.INTERRUPT)
REQUEST_TYPE_MASK = 0x60
REQUEST_TYPE_STANDARD = 0x00

LINUX_ENOENT = 2
LINUX_ENODEV = 19
LINUX_EPIPE = 32
LINUX_EOVERFLOW = 75
LINUX_ESHUTDOWN = 108
LINUX_ETIMEDOUT = 110

LIBUSB_ERROR_IO = -1
LIBUSB_ERROR_NO_DEVICE = -4
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_OVERFLOW = -8
LIBUSB_ERROR_PIPE = -9


@dataclass(frozen=True)
class FakeEndpoint:
    bEndpointAddress: int
    bmAttributes: int
    wMaxPacketSize: int


@dataclass
class FakeInterface:
    bInterfaceNumber: int
    bAlternateSetting: int
    bInterfaceClass: int
    endpoints: list[FakeEndpoint] = field(default_factory=list)

    def __iter__(self) -> Iterator[FakeEndpoint]:
        return iter(self.endpoints)


@dataclass
class FakeConfiguration:
    bConfigurationValue: int
    interfaces: list[FakeInterface] = field(default_factory=list)

    def __getitem__(self, interface_and_alternate: tuple[int, int]) -> FakeInterface:
        for interface in self.interfaces:
            if (interface.bInterfaceNumber, interface.bAlternateSetting) == interface_and_alternate:
                return interface
        raise KeyError(interface_and_alternate)

    def __iter__(self) -> Iterator[FakeInterface]:
        return iter(self.interfaces)


def parse_configuration_descriptor(descriptor: bytes) -> FakeConfiguration:
    configuration = FakeConfiguration(bConfigurationValue=descriptor[5])
    offset = 0
    while offset + 2 <= len(descriptor):
        length, descriptor_type = descriptor[offset], descriptor[offset + 1]
        if length == 0:
            break
        if descriptor_type == DESCRIPTOR_TYPE_INTERFACE:
            configuration.interfaces.append(
                FakeInterface(descriptor[offset + 2], descriptor[offset + 3], descriptor[offset + 5]))
        elif descriptor_type == DESCRIPTOR_TYPE_ENDPOINT:
            max_packet_size = struct.unpack_from("<H", descriptor, offset + 4)[0]
            configuration.interfaces[-1].endpoints.append(
                FakeEndpoint(descriptor[offset + 2], descriptor[offset + 3], max_packet_size))
        offset += length
    return configuration


def usb_error_for_status(status: int) -> usb.core.USBError:
    """The error pyusb's libusb1 backend raises for a URB that completed with ``status`` (-Linux errno)."""
    if status in (-LINUX_ENOENT, -LINUX_ETIMEDOUT):
        return usb.core.USBTimeoutError("Operation timed out", LIBUSB_ERROR_TIMEOUT, errno.ETIMEDOUT)
    if status == -LINUX_EPIPE:
        return usb.core.USBError("Pipe error", LIBUSB_ERROR_PIPE, errno.EPIPE)
    if status in (-LINUX_ENODEV, -LINUX_ESHUTDOWN):
        return usb.core.USBError("No such device (it may have been disconnected)",
                                 LIBUSB_ERROR_NO_DEVICE, errno.ENODEV)
    if status == -LINUX_EOVERFLOW:
        return usb.core.USBError("Overflow", LIBUSB_ERROR_OVERFLOW, errno.EOVERFLOW)
    return usb.core.USBError("Input/Output Error", LIBUSB_ERROR_IO, errno.EIO)


class FakeUsbDevice:
    def __init__(self, walk: CaptureWalk, device_descriptor: bytes, configuration_descriptor: bytes,
                 bus: int, address: int) -> None:
        self.walk = walk
        self.bus = bus
        self.address = address
        self.idVendor, self.idProduct, self.bcdDevice = struct.unpack_from("<HHH", device_descriptor, 8)
        self.configuration = parse_configuration_descriptor(configuration_descriptor)
        self.transfer_type_by_endpoint: dict[int, TransferType] = {}
        for interface in self.configuration:
            for endpoint in interface:
                self.transfer_type_by_endpoint[endpoint.bEndpointAddress] = (
                    TRANSFER_TYPE_BY_ENDPOINT_ATTRIBUTES[endpoint.bmAttributes & 0x03])

    def ctrl_transfer(self, bmRequestType: int, bRequest: int, wValue: int = 0, wIndex: int = 0,
                      data_or_wLength: int | bytes | array[int] | None = None,
                      timeout: int | None = None) -> array[int] | int:
        is_in = bool(bmRequestType & 0x80)
        if is_in:
            assert isinstance(data_or_wLength, int)
            length = data_or_wLength
            data = b""
        else:
            data = b"" if data_or_wLength is None else bytes(data_or_wLength)  # type: ignore[arg-type]
            length = len(data)
        if bmRequestType & REQUEST_TYPE_MASK == REQUEST_TYPE_STANDARD and not is_in:
            return length
        request = UsbTransfer(
            frame_number=0, bus=self.bus, device_address=self.address,
            endpoint=0x80 if is_in else 0x00, transfer_type=TransferType.CONTROL,
            setup=struct.pack("<BBHHH", bmRequestType, bRequest, wValue, wIndex, length),
            data=data, status=0, timestamp=0.0)
        captured = self.walk.claim(request, current_producer_name())
        if captured is None:
            raise usb_error_for_status(-LINUX_ENODEV)
        if captured.status != 0:
            raise usb_error_for_status(captured.status)
        if is_in:
            return array("B", captured.data)
        return length

    def write(self, endpoint: int, data: bytes | array[int], timeout: int | None = None) -> int:
        request = UsbTransfer(
            frame_number=0, bus=self.bus, device_address=self.address, endpoint=endpoint,
            transfer_type=self.transfer_type_by_endpoint[endpoint], setup=None, data=bytes(data),
            status=0, timestamp=0.0)
        captured = self.walk.claim(request, current_producer_name())
        if captured is None:
            raise usb_error_for_status(-LINUX_ENODEV)
        if captured.status != 0:
            raise usb_error_for_status(captured.status)
        return len(data)

    def read(self, endpoint: int, size_or_buffer: int | array[int],
             timeout: int | None = None) -> array[int]:
        captured = self.walk.read(endpoint, current_producer_name())
        if captured is None:
            raise usb_error_for_status(-LINUX_ETIMEDOUT)
        if captured.status != 0:
            raise usb_error_for_status(captured.status)
        return array("B", captured.data)

    def get_active_configuration(self) -> FakeConfiguration:
        return self.configuration

    def set_configuration(self, configuration: int | None = None) -> None:
        return None

    def set_interface_altsetting(self, interface: int | None = None,
                                 alternate_setting: int | None = None) -> None:
        return None

    def is_kernel_driver_active(self, interface: int) -> bool:
        return False

    def detach_kernel_driver(self, interface: int) -> None:
        return None

    def attach_kernel_driver(self, interface: int) -> None:
        return None

    def clear_halt(self, endpoint: int) -> None:
        return None

    def reset(self) -> None:
        return None
