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
    assert decoder.is_driver_vendor_request([control_read(C.MT_REQ_IN_VENDOR, C.MT_VEND_READ_EXT, 0x8000, 0x0008)], 0)
    assert decoder.is_driver_vendor_request([control_read(C.MT_REQ_IN_UHW_VENDOR, C.MT_VEND_DEV_MODE, 0x7001, 0x0200)], 0)


def test_bluetooth_vendor_requests_on_the_shared_device_are_not_the_drivers() -> None:
    decoder = MediatekConnacDecoder()
    assert not decoder.is_driver_vendor_request([control_read(BLUETOOTH_REQUEST_TYPE, C.MT_VEND_READ_EXT, 0x8000, 0x0008)], 0)
    assert not decoder.is_driver_vendor_request([control_read(BLUETOOTH_REQUEST_TYPE, C.MT_VEND_DEV_MODE, 0x30, 0)], 0)


def uhw_access(request: int, value: int) -> UsbOp:
    if request == C.MT_VEND_WRITE:
        request_type, endpoint = C.MT_REQ_OUT_UHW_VENDOR, 0x00
    else:
        request_type, endpoint = C.MT_REQ_IN_UHW_VENDOR, 0x80
    address = C.MT_SSUSB_EPCTL_CSR_EP_RST_OPT
    setup = struct.pack("<BBHHH", request_type, request, address >> 16, address & 0xFFFF, 4)
    return UsbOp(frame_number=0, bus=1, device_address=2, endpoint=endpoint, transfer_type=TransferType.CONTROL,
                 setup=setup, data=value.to_bytes(4, "little"), status=0, submit_time=0.0, completion_time=0.0)


def test_ep_rst_opt_read_modify_write_is_the_drivers() -> None:
    ops = [uhw_access(C.MT_VEND_DEV_MODE, 0xFFFEFFFF), uhw_access(C.MT_VEND_WRITE, 0xFF8EFC0F)]
    assert MediatekConnacDecoder().is_driver_vendor_request(ops, 1)


def test_bluetooth_ep_rst_opt_write_is_not_the_drivers() -> None:
    decoder = MediatekConnacDecoder()
    bluetooth_write = uhw_access(C.MT_VEND_WRITE, 0x00010001)
    assert not decoder.is_driver_vendor_request([bluetooth_write], 0)
    ops = [uhw_access(C.MT_VEND_DEV_MODE, 0x1), uhw_access(C.MT_VEND_WRITE, 0x1), bluetooth_write]
    assert decoder.is_driver_vendor_request(ops, 1)
    assert not decoder.is_driver_vendor_request(ops, 2)


def uni_mcu_command(command_id: int, payload: bytes) -> UsbOp:
    txd = bytearray(48)
    struct.pack_into("<H", txd, 34, command_id)
    message = struct.pack("<I", 48 + len(payload)) + bytes(txd) + payload
    padded = message + bytes(-len(message) % 4 + 4)
    return UsbOp(frame_number=0, bus=1, device_address=2, endpoint=C.EP_OUT_MCU, transfer_type=TransferType.BULK,
                 setup=None, data=padded, status=0, submit_time=0.0, completion_time=0.0)


def test_mcu_command_width_is_its_payload_without_the_usb_padding() -> None:
    register_ops = MediatekConnacDecoder().register_ops(uni_mcu_command(C.MCU_UNI_CMD_BAND_CONFIG, b"\x01" + bytes(5)))
    assert [(register_op.value, register_op.width) for register_op in register_ops] == [(1, 6)]
