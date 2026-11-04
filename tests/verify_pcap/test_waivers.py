from __future__ import annotations

import struct

from tests.verify_pcap.ath9k_htc_decoder import Ath9kHtcDecoder
from tests.verify_pcap.realtek_decoder import HALMAC_TX_DESC_SW_SEQ, REALTEK_USB_CMD_REQ, RealtekDecoder
from tests.verify_pcap.rt2800_decoder import Rt2800Decoder
from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.verify_capture import (RegisterAccess, RegisterOp, TxOpField, VerifyCapture, Waiver,
                                              WaiverReason)
from wifit3.chips.ar9271_v2 import constants as C, reg as R, wmi

OTHER_REGISTER = 0x7014
READ_REGISTER = 0x803C
REALTEK_WAIVED_REGISTER = 0x0560
REALTEK_OTHER_REGISTER = 0x0100


def wmi_op(endpoint: int, command_id: int, seq: int, words: list[int]) -> UsbOp:
    payload = struct.pack(">HH", command_id, seq) + b"".join(struct.pack(">I", word) for word in words)
    htc_header = struct.pack(">BBH4s", 1, 0, len(payload), bytes(4))
    return UsbOp(frame_number=0, bus=1, device_address=2, endpoint=endpoint, transfer_type=TransferType.INTERRUPT,
                 setup=None, data=htc_header + payload, status=0, submit_time=0.0, completion_time=0.0)


def reg_write(seq: int, words: list[int]) -> UsbOp:
    return wmi_op(C.EP_REG_OUT, wmi.WMI_REG_WRITE_CMDID, seq, words)


def reg_read(seq: int, addresses: list[int]) -> UsbOp:
    return wmi_op(C.EP_REG_OUT, wmi.WMI_REG_READ_CMDID, seq, addresses)


def response(seq: int) -> UsbOp:
    return wmi_op(C.EP_REG_IN, wmi.WMI_REG_WRITE_CMDID, seq, [])


def realtek_read(address: int, value: int) -> UsbOp:
    setup = struct.pack("<BBHHH", 0xC0, REALTEK_USB_CMD_REQ, address, 0, 4)
    return UsbOp(frame_number=0, bus=1, device_address=2, endpoint=0x80, transfer_type=TransferType.CONTROL,
                 setup=setup, data=value.to_bytes(4, "little"), status=0, submit_time=0.0, completion_time=0.0)


def realtek_write(address: int, value: int) -> UsbOp:
    setup = struct.pack("<BBHHH", 0x40, REALTEK_USB_CMD_REQ, address, 0, 4)
    return UsbOp(frame_number=0, bus=1, device_address=2, endpoint=0x00, transfer_type=TransferType.CONTROL,
                 setup=setup, data=value.to_bytes(4, "little"), status=0, submit_time=0.0, completion_time=0.0)


class TsfValueOnly(VerifyCapture):
    DECODER = Ath9kHtcDecoder()
    WAIVERS = [Waiver(WaiverReason.HOST_VALUE, "synthetic", value_only_register=R.AR_TSF_L32)]


RUN = (RegisterOp(RegisterAccess.READ, READ_REGISTER, None),
       RegisterOp(RegisterAccess.WRITE, OTHER_REGISTER, 0x2),
       RegisterOp(RegisterAccess.WRITE, R.AR_TSF_U32, 0x0))


class CaptureOnlyRun(VerifyCapture):
    DECODER = Ath9kHtcDecoder()
    WAIVERS = [Waiver(WaiverReason.CAPTURE_SETUP, "synthetic", capture_only_run=RUN)]


def test_value_only_accepts_a_different_waived_value() -> None:
    captured = reg_write(7, [OTHER_REGISTER, 0x1, R.AR_TSF_L32, 0x007D1AAA])
    request = reg_write(7, [OTHER_REGISTER, 0x1, R.AR_TSF_L32, 0x007CFD9D])
    assert TsfValueOnly().is_same_request_except_waived_values(captured, request, TsfValueOnly.WAIVERS)


def test_value_only_compares_the_other_values_in_the_op() -> None:
    captured = reg_write(7, [OTHER_REGISTER, 0x1, R.AR_TSF_L32, 0x007D1AAA])
    request = reg_write(7, [OTHER_REGISTER, 0x2, R.AR_TSF_L32, 0x007CFD9D])
    assert not TsfValueOnly().is_same_request_except_waived_values(captured, request, TsfValueOnly.WAIVERS)


def test_value_only_compares_the_seq() -> None:
    captured = reg_write(7, [R.AR_TSF_L32, 0x007D1AAA])
    request = reg_write(8, [R.AR_TSF_L32, 0x007CFD9D])
    assert not TsfValueOnly().is_same_request_except_waived_values(captured, request, TsfValueOnly.WAIVERS)


def test_value_only_compares_the_address() -> None:
    captured = reg_write(7, [R.AR_TSF_L32, 0x007D1AAA])
    request = reg_write(7, [R.AR_TSF_U32, 0x007D1AAA])
    assert not TsfValueOnly().is_same_request_except_waived_values(captured, request, TsfValueOnly.WAIVERS)


def test_value_only_compares_other_registers_values() -> None:
    captured = reg_write(7, [OTHER_REGISTER, 0x007D1AAA])
    request = reg_write(7, [OTHER_REGISTER, 0x007CFD9D])
    assert not TsfValueOnly().is_same_request_except_waived_values(captured, request, TsfValueOnly.WAIVERS)


def test_capture_only_run_matches_the_exact_run_with_in_ops_between() -> None:
    ops = [reg_write(1, [R.AR_TSF_L32, 0x5]), response(1),
           reg_read(2, [READ_REGISTER]), response(2),
           reg_write(3, [OTHER_REGISTER, 0x2, R.AR_TSF_U32, 0x0]), response(3),
           reg_write(4, [OTHER_REGISTER, 0x3])]
    assert CaptureOnlyRun().capture_only_run_positions(ops, RUN) == [2, 4]


def test_capture_only_run_does_not_match_a_different_value() -> None:
    ops = [reg_read(2, [READ_REGISTER]), reg_write(3, [OTHER_REGISTER, 0x2, R.AR_TSF_U32, 0x1])]
    assert CaptureOnlyRun().capture_only_run_positions(ops, RUN) == []


def test_capture_only_run_does_not_match_a_part_of_an_op() -> None:
    ops = [reg_read(2, [READ_REGISTER]), reg_write(3, [OTHER_REGISTER, 0x2, R.AR_TSF_U32, 0x0, OTHER_REGISTER, 0x4])]
    assert CaptureOnlyRun().capture_only_run_positions(ops, RUN) == []


def test_capture_only_run_does_not_match_across_an_op_without_register_ops() -> None:
    ops = [reg_read(2, [READ_REGISTER]), wmi_op(C.EP_REG_OUT, wmi.WMI_ENABLE_INTR_CMDID, 3, []),
           reg_write(4, [OTHER_REGISTER, 0x2, R.AR_TSF_U32, 0x0])]
    assert CaptureOnlyRun().capture_only_run_positions(ops, RUN) == []


def test_capture_only_run_matches_every_occurrence() -> None:
    ops = [reg_read(2, [READ_REGISTER]), reg_write(3, [OTHER_REGISTER, 0x2, R.AR_TSF_U32, 0x0]),
           reg_write(4, [OTHER_REGISTER, 0x3]),
           reg_read(5, [READ_REGISTER]), reg_write(6, [OTHER_REGISTER, 0x2, R.AR_TSF_U32, 0x0])]
    assert CaptureOnlyRun().capture_only_run_positions(ops, RUN) == [0, 1, 3, 4]


class RealtekValueOnly(VerifyCapture):
    DECODER = RealtekDecoder()
    WAIVERS = [Waiver(WaiverReason.HOST_VALUE, "synthetic", value_only_register=REALTEK_WAIVED_REGISTER)]


REALTEK_RUN = (RegisterOp(RegisterAccess.READ, REALTEK_OTHER_REGISTER, None),
               RegisterOp(RegisterAccess.WRITE, REALTEK_OTHER_REGISTER, 0x2))


class RealtekCaptureOnlyRun(VerifyCapture):
    DECODER = RealtekDecoder()
    WAIVERS = [Waiver(WaiverReason.CAPTURE_SETUP, "synthetic", capture_only_run=REALTEK_RUN)]


def test_realtek_value_only_accepts_a_different_waived_value() -> None:
    captured = realtek_write(REALTEK_WAIVED_REGISTER, 0x007D1AAA)
    request = realtek_write(REALTEK_WAIVED_REGISTER, 0x007CFD9D)
    assert RealtekValueOnly().is_same_request_except_waived_values(captured, request, RealtekValueOnly.WAIVERS)


def test_realtek_value_only_compares_other_registers_values() -> None:
    captured = realtek_write(REALTEK_OTHER_REGISTER, 0x007D1AAA)
    request = realtek_write(REALTEK_OTHER_REGISTER, 0x007CFD9D)
    assert not RealtekValueOnly().is_same_request_except_waived_values(captured, request, RealtekValueOnly.WAIVERS)


def test_realtek_capture_only_run_matches_a_read_and_a_write() -> None:
    ops = [realtek_write(REALTEK_OTHER_REGISTER, 0x1),
           realtek_read(REALTEK_OTHER_REGISTER, 0x1), realtek_write(REALTEK_OTHER_REGISTER, 0x2),
           realtek_read(REALTEK_OTHER_REGISTER, 0x2), realtek_write(REALTEK_OTHER_REGISTER, 0x3)]
    assert RealtekCaptureOnlyRun().capture_only_run_positions(ops, REALTEK_RUN) == [1, 2]


def realtek_tx(sw_seq: int, checksum: int) -> UsbOp:
    words = [0] * 12
    words[0] = 0x3005002A
    words[7] = checksum
    words[9] = sw_seq << 12
    data = struct.pack("<12I", *words) + bytes(24)
    return UsbOp(frame_number=0, bus=1, device_address=2, endpoint=0x05, transfer_type=TransferType.BULK,
                 setup=None, data=data, status=0, submit_time=0.0, completion_time=0.0)


class RealtekSwSeqValueOnly(VerifyCapture):
    DECODER = RealtekDecoder(tx_descriptor_size=48, tx_descriptor_checksum_size=48)
    WAIVERS = [Waiver(WaiverReason.WIFIT3_ONLY, "synthetic", value_only_tx_op_field=HALMAC_TX_DESC_SW_SEQ)]


def test_tx_op_field_accepts_a_different_value_with_its_checksum() -> None:
    captured = realtek_tx(sw_seq=0, checksum=0x3005 ^ 0x002A)
    request = realtek_tx(sw_seq=1, checksum=0x3005 ^ 0x002A ^ 0x1000)
    assert RealtekSwSeqValueOnly().is_same_request_except_waived_values(captured, request, RealtekSwSeqValueOnly.WAIVERS)


def test_tx_op_field_compares_the_checksum() -> None:
    captured = realtek_tx(sw_seq=0, checksum=0x3005 ^ 0x002A)
    request = realtek_tx(sw_seq=1, checksum=0x3005 ^ 0x002A)
    assert not RealtekSwSeqValueOnly().is_same_request_except_waived_values(captured, request, RealtekSwSeqValueOnly.WAIVERS)


class RealtekSwSeqOutsideChecksumValueOnly(VerifyCapture):
    DECODER = RealtekDecoder(tx_descriptor_size=48)
    WAIVERS = [Waiver(WaiverReason.WIFIT3_ONLY, "synthetic", value_only_tx_op_field=HALMAC_TX_DESC_SW_SEQ)]


def test_tx_op_field_outside_the_checksum_span_leaves_the_checksum() -> None:
    captured = realtek_tx(sw_seq=0, checksum=0x3005 ^ 0x002A)
    request = realtek_tx(sw_seq=1, checksum=0x3005 ^ 0x002A)
    assert RealtekSwSeqOutsideChecksumValueOnly().is_same_request_except_waived_values(
        captured, request, RealtekSwSeqOutsideChecksumValueOnly.WAIVERS)


RT2800_TXWI_W1_ACK = TxOpField(dword=2, shift=0, width=1)


def rt2800_tx(txwi_word1: int) -> UsbOp:
    data = struct.pack("<5I", 0x00000020, 0, txwi_word1, 0, 0) + bytes(12)
    return UsbOp(frame_number=0, bus=1, device_address=2, endpoint=0x01, transfer_type=TransferType.BULK,
                 setup=None, data=data, status=0, submit_time=0.0, completion_time=0.0)


class Rt2800AckValueOnly(VerifyCapture):
    DECODER = Rt2800Decoder()
    WAIVERS = [Waiver(WaiverReason.CAPTURE_SETUP, "synthetic", value_only_tx_op_field=RT2800_TXWI_W1_ACK)]


def test_rt2800_tx_op_field_accepts_a_different_value() -> None:
    captured = rt2800_tx(txwi_word1=0x000C0000)
    request = rt2800_tx(txwi_word1=0x000C0001)
    assert Rt2800AckValueOnly().is_same_request_except_waived_values(captured, request, Rt2800AckValueOnly.WAIVERS)


def test_rt2800_tx_op_field_compares_the_other_bits() -> None:
    captured = rt2800_tx(txwi_word1=0x000C0000)
    request = rt2800_tx(txwi_word1=0x000D0001)
    assert not Rt2800AckValueOnly().is_same_request_except_waived_values(captured, request, Rt2800AckValueOnly.WAIVERS)


REALTEK_ANY_VALUE_RUN = (RegisterOp(RegisterAccess.READ, REALTEK_OTHER_REGISTER, None),
                         RegisterOp(RegisterAccess.WRITE, REALTEK_OTHER_REGISTER, None))


def test_realtek_capture_only_run_value_none_matches_any_value() -> None:
    ops = [realtek_read(REALTEK_OTHER_REGISTER, 0x1), realtek_write(REALTEK_OTHER_REGISTER, 0x5),
           realtek_write(REALTEK_WAIVED_REGISTER, 0x5)]
    assert RealtekCaptureOnlyRun().capture_only_run_positions(ops, REALTEK_ANY_VALUE_RUN) == [0, 1]


class RealtekDriverOnlyRun(VerifyCapture):
    DECODER = RealtekDecoder()
    WAIVERS = [Waiver(WaiverReason.WIFIT3_ONLY, "synthetic", driver_only_run=REALTEK_RUN)]


def test_realtek_driver_only_run_starts_where_the_capture_differs_then_claims_its_ops_in_order_once() -> None:
    waiver = RealtekDriverOnlyRun.WAIVERS[0]
    progress = {waiver: 0}
    test = RealtekDriverOnlyRun()
    assert not test.is_driver_only_request(realtek_write(REALTEK_OTHER_REGISTER, 0x2), True, progress)
    assert not test.is_driver_only_request(realtek_read(REALTEK_OTHER_REGISTER, 0x0), False, progress)
    assert test.is_driver_only_request(realtek_read(REALTEK_OTHER_REGISTER, 0x0), True, progress)
    assert test.is_driver_only_request(realtek_write(REALTEK_OTHER_REGISTER, 0x2), False, progress)
    assert not test.is_driver_only_request(realtek_read(REALTEK_OTHER_REGISTER, 0x0), True, progress)
    assert progress[waiver] == 2
