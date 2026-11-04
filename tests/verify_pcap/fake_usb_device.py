"""A pyusb Device stand-in whose ops are reproduced from a CaptureReplay; libusb is never loaded."""
from __future__ import annotations

import errno
import struct
from array import array
from dataclasses import dataclass, field, replace
from typing import Iterator

import usb.core

from tests.verify_pcap.capture_replay import CaptureReplay
from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.virtual_clock_loop import current_caller_name
from wifit3.models import DeviceID

GET_DESCRIPTOR = 6
DESCRIPTOR_TYPE_DEVICE = 1
DESCRIPTOR_TYPE_CONFIGURATION = 2
DESCRIPTOR_TYPE_INTERFACE = 4
DESCRIPTOR_TYPE_ENDPOINT = 5
# Endpoint descriptor bmAttributes order differs from usbmon's op-type order.
TRANSFER_TYPE_BY_ENDPOINT_ATTRIBUTES = (TransferType.CONTROL, TransferType.ISOCHRONOUS,
                                        TransferType.BULK, TransferType.INTERRUPT)
REQUEST_TYPE_MASK = 0x60
REQUEST_TYPE_STANDARD = 0x00
REQUEST_TYPE_CLASS = 0x20
INTERFACE_CLASS_VENDOR_SPECIFIC = 0xFF

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


def find_chip_descriptors(ops: list[UsbOp], supported_ids: list[DeviceID]
                          ) -> tuple[DeviceID, bytes, bytes] | None:
    """The chip's DeviceID and its device + configuration descriptors, read by the kernel at plug-in."""
    device_id: DeviceID | None = None
    device_descriptor = b""
    configuration_descriptor = b""
    for op in ops:
        if op.transfer_type != TransferType.CONTROL or op.setup is None:
            continue
        if op.request != GET_DESCRIPTOR or not op.is_in or op.device_address == 0:
            continue
        descriptor_type = op.value >> 8
        if descriptor_type == DESCRIPTOR_TYPE_DEVICE and len(op.data) >= 18:
            vid, pid = struct.unpack_from("<HH", op.data, 8)
            for supported in supported_ids:
                if (supported.vid, supported.pid) == (vid, pid):
                    device_id = replace(supported, bus=op.bus, address=op.device_address)
                    device_descriptor = op.data
        elif (descriptor_type == DESCRIPTOR_TYPE_CONFIGURATION and device_id is not None
              and (op.bus, op.device_address) == (device_id.bus, device_id.address)
              and len(op.data) == struct.unpack_from("<H", op.data, 2)[0]):
            configuration_descriptor = op.data
    if device_id is None or not configuration_descriptor:
        return None
    return device_id, device_descriptor, configuration_descriptor


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
    def __init__(self, replay: CaptureReplay, device_descriptor: bytes, configuration_descriptor: bytes,
                 bus: int, address: int) -> None:
        self.replay = replay
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
        request = UsbOp(
            frame_number=0, bus=self.bus, device_address=self.address,
            endpoint=0x80 if is_in else 0x00, transfer_type=TransferType.CONTROL,
            setup=struct.pack("<BBHHH", bmRequestType, bRequest, wValue, wIndex, length),
            data=data, status=0, submit_time=0.0, completion_time=0.0)
        captured = self.replay.match_request(request, current_caller_name())
        if captured is None:  # close() past the end of the capture: not compared
            return array("B", bytes(length)) if is_in else length
        if captured.status != 0:
            raise usb_error_for_status(captured.status)
        if is_in:
            return array("B", captured.data)
        return length

    def write(self, endpoint: int, data: bytes | array[int], timeout: int | None = None) -> int:
        request = UsbOp(
            frame_number=0, bus=self.bus, device_address=self.address, endpoint=endpoint,
            transfer_type=self.transfer_type_by_endpoint[endpoint], setup=None, data=bytes(data),
            status=0, submit_time=0.0, completion_time=0.0)
        captured = self.replay.match_request(request, current_caller_name())
        if captured is None:  # close() past the end of the capture: not compared
            return len(data)
        if captured.status != 0:
            raise usb_error_for_status(captured.status)
        return len(data)

    def read(self, endpoint: int, size_or_buffer: int | array[int],
             timeout: int | None = None) -> array[int]:
        captured = self.replay.read(endpoint, current_caller_name())
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
