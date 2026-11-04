from __future__ import annotations

import struct

from tests.verify_pcap.ath9k_htc_decoder import Ath9kHtcDecoder
from tests.verify_pcap.usb_capture import TransferType, UsbOp
from tests.verify_pcap.verify_capture import RegisterAccess, RegisterOp, VerifyCapture, Waiver, WaiverReason
from wifit3.chips.ar9271_v2 import constants as C, reg as R, wmi

OTHER_REGISTER = 0x7014
READ_REGISTER = 0x803C


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
    assert TsfValueOnly().is_same_request_except_waived_values(captured, request)


def test_value_only_compares_the_other_values_in_the_op() -> None:
    captured = reg_write(7, [OTHER_REGISTER, 0x1, R.AR_TSF_L32, 0x007D1AAA])
    request = reg_write(7, [OTHER_REGISTER, 0x2, R.AR_TSF_L32, 0x007CFD9D])
    assert not TsfValueOnly().is_same_request_except_waived_values(captured, request)


def test_value_only_compares_the_seq() -> None:
    captured = reg_write(7, [R.AR_TSF_L32, 0x007D1AAA])
    request = reg_write(8, [R.AR_TSF_L32, 0x007CFD9D])
    assert not TsfValueOnly().is_same_request_except_waived_values(captured, request)


def test_value_only_compares_the_address() -> None:
    captured = reg_write(7, [R.AR_TSF_L32, 0x007D1AAA])
    request = reg_write(7, [R.AR_TSF_U32, 0x007D1AAA])
    assert not TsfValueOnly().is_same_request_except_waived_values(captured, request)


def test_value_only_compares_other_registers_values() -> None:
    captured = reg_write(7, [OTHER_REGISTER, 0x007D1AAA])
    request = reg_write(7, [OTHER_REGISTER, 0x007CFD9D])
    assert not TsfValueOnly().is_same_request_except_waived_values(captured, request)


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
