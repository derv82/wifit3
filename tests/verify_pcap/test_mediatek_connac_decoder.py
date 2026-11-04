from __future__ import annotations

import struct

from tests.verify_pcap.mediatek_connac_decoder import MediatekConnacDecoder
from tests.verify_pcap.usb_capture import TransferType, UsbOp
from wifit3.chips.mt7925au import constants as C

BLUETOOTH_REQUEST_TYPE = 0xC0


def control_read(request_type: int, request: int, value: int, index: int) -> UsbOp:
    setup = struct.pack("<BBHHH", request_type, request, value, index, 4)
    return UsbOp(frame_number=0, bus=1, device_address=2, endpoint=0x80, transfer_type=TransferType.CONTROL,
                 setup=setup, data=bytes(4), status=0, submit_time=0.0, completion_time=0.0)


def test_mt76_request_types_are_the_drivers() -> None:
    decoder = MediatekConnacDecoder()
    assert decoder.is_driver_vendor_request(control_read(C.MT_REQ_IN_VENDOR, C.MT_VEND_READ_EXT, 0x8000, 0x0008))
    assert decoder.is_driver_vendor_request(control_read(C.MT_REQ_IN_UHW_VENDOR, C.MT_VEND_DEV_MODE, 0x7001, 0x0200))


def test_bluetooth_vendor_requests_on_the_shared_device_are_not_the_drivers() -> None:
    decoder = MediatekConnacDecoder()
    assert not decoder.is_driver_vendor_request(control_read(BLUETOOTH_REQUEST_TYPE, C.MT_VEND_READ_EXT, 0x8000, 0x0008))
    assert not decoder.is_driver_vendor_request(control_read(BLUETOOTH_REQUEST_TYPE, C.MT_VEND_DEV_MODE, 0x30, 0))
