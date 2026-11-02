"""MCU message framing and firmware-header tests for the MT7601U port.

No hardware: the transport is faked, so this covers the byte layout and the
header validation. The wire bytes themselves are checked against the cold-boot
capture by scripts/chips/mt7601u/verify_mt7601u.py.
"""
from __future__ import annotations

import pytest

from wifit3.chips.mt7601u import constants as C
from wifit3.chips.mt7601u.firmware import (
    FirmwareError,
    FW_HEADER_LEN,
    describe,
    validate,
)
from wifit3.chips.mt7601u.mcu import (
    MCU_FW_URB_MAX_PAYLOAD,
    MT7601UMcu,
    McuTimeout,
    wrap_mcu_msg,
)


class FakeTransport:
    """Records every wire op and answers MCU commands with a matching CMD_DONE."""

    def __init__(self, com_reg0: list[int] | None = None):
        self.ops: list[tuple] = []
        self.com_reg0 = list(com_reg0 or [1])
        self.payloads: list[bytes] = []

    def rr(self, offset: int) -> int:
        if offset == C.MT_MCU_COM_REG0:
            return self.com_reg0.pop(0) if len(self.com_reg0) > 1 else self.com_reg0[0]
        return 0

    def wr(self, offset: int, val: int) -> None:
        self.ops.append(("wr", offset, val))

    def fce_wr(self, offset: int, val: int) -> None:
        self.ops.append(("fce", offset, val))

    def bulk_out(self, data: bytes, timeout_ms: int = 0) -> int:
        self.ops.append(("bulk", len(data)))
        self.payloads.append(data)
        return len(data)

    def bulk_out_inband_fw(self, data: bytes, timeout_ms: int = 0) -> int:
        self.ops.append(("fw", len(data)))
        return len(data)

    def bulk_in_resp(self, buf: int, timeout_ms: int = 0) -> bytes:
        """Echo CMD_DONE with the seq the port last sent (mcu.c:96-98)."""
        if not self.payloads:
            return b""
        info = int.from_bytes(self.payloads[-1][:4], "little")
        seq = C._field_get(C.MT_TXD_CMD_INFO_SEQ, info)
        done = C._field_prep(C.MT_RXD_CMD_INFO_CMD_SEQ, seq) | \
            C._field_prep(C.MT_RXD_CMD_INFO_EVT_TYPE, C.CMD_DONE)
        return done.to_bytes(4, "little")


class TestWrapMcuMsg:
    def test_layout_is_descriptor_payload_pad_tail(self) -> None:
        msg = wrap_mcu_msg(b"\xaa" * 6, seq=3, cmd=C.CMD_FUN_SET_OP)
        # 4B descriptor + payload padded to 8 + 4B trailing zero
        assert len(msg) == 4 + 8 + 4
        assert msg[-4:] == b"\x00\x00\x00\x00"
        assert msg[4:10] == b"\xaa" * 6

    def test_descriptor_carries_seq_cmd_port_and_padded_length(self) -> None:
        msg = wrap_mcu_msg(b"\x01\x02\x03\x04\x05\x06", seq=3, cmd=C.CMD_FUN_SET_OP)
        info = int.from_bytes(msg[:4], "little")
        assert C._field_get(C.MT_TXD_CMD_INFO_SEQ, info) == 3
        assert C._field_get(C.MT_TXD_CMD_INFO_TYPE, info) == C.CMD_FUN_SET_OP
        assert C._field_get(C.MT_TXD_INFO_D_PORT, info) == C.CPU_TX_PORT
        assert C._field_get(C.MT_TXD_INFO_TYPE, info) == C.DMA_COMMAND
        assert C._field_get(C.MT_TXD_INFO_LEN, info) == 8      # round_up(6, 4)

    def test_length_field_is_four_byte_aligned(self) -> None:
        for payload_len in (1, 2, 3, 4, 5):
            msg = wrap_mcu_msg(b"\x00" * payload_len, seq=0, cmd=1)
            info = int.from_bytes(msg[:4], "little")
            assert C._field_get(C.MT_TXD_INFO_LEN, info) % 4 == 0
            assert len(msg) == 4 + ((payload_len + 3) & ~3) + 4


class TestMsgSend:
    def test_seq_allocated_from_nibble_and_never_zero(self) -> None:
        tp = FakeTransport()
        mcu = MT7601UMcu(tp)
        mcu.mcu_running = True
        seen = []
        for _ in range(18):
            # func 5 is the only one function_select response-awaits (mcu.c:170).
            mcu.function_select(C.ATOMIC_TSSI_SETTING, 1)
            info = int.from_bytes(tp.payloads[-1][:4], "little")
            seen.append(C._field_get(C.MT_TXD_CMD_INFO_SEQ, info))
        assert seen == list(range(1, 16)) + [1, 2, 3]
        assert 0 not in seen

    def test_function_select_awaits_a_response_only_for_func_5(self) -> None:
        tp = FakeTransport()
        mcu = MT7601UMcu(tp)
        mcu.mcu_running = True
        mcu.function_select(C.Q_SELECT, 1)
        info = int.from_bytes(tp.payloads[-1][:4], "little")
        assert C._field_get(C.MT_TXD_CMD_INFO_SEQ, info) == 0

    def test_fire_and_forget_command_sends_seq_zero(self) -> None:
        tp = FakeTransport()
        mcu = MT7601UMcu(tp)
        mcu.mcu_running = True
        mcu.msg_send(b"\x00" * 4, C.CMD_RANDOM_WRITE, wait_resp=False)
        info = int.from_bytes(tp.payloads[-1][:4], "little")
        assert C._field_get(C.MT_TXD_CMD_INFO_SEQ, info) == 0

    def test_send_before_the_mcu_is_running_is_refused(self) -> None:
        mcu = MT7601UMcu(FakeTransport())
        with pytest.raises(McuTimeout, match="not running"):
            mcu.msg_send(b"\x00" * 4, C.CMD_RANDOM_WRITE, wait_resp=False)

    def test_write_reg_pairs_sends_one_message(self) -> None:
        tp = FakeTransport()
        mcu = MT7601UMcu(tp)
        mcu.mcu_running = True
        mcu.write_reg_pairs(0, [(1, 0xAA), (2, 0xBB)])
        assert [op for op in tp.ops if op[0] == "bulk"] == [("bulk", 4 + 8 + 8 + 4)]

    def test_write_reg_pairs_chunks_at_inband_max(self) -> None:
        tp = FakeTransport()
        mcu = MT7601UMcu(tp)
        mcu.mcu_running = True
        per_msg = C.INBAND_PACKET_MAX_LEN // 8
        mcu.write_reg_pairs(0, [(i, i) for i in range(per_msg + 1)])
        # A message holds one address+value pair per 8 bytes; 192//8 = 24 pairs max.
        assert len([op for op in tp.ops if op[0] == "bulk"]) == 2

    def test_burst_write_carries_a_leading_address(self) -> None:
        tp = FakeTransport()
        mcu = MT7601UMcu(tp)
        mcu.mcu_running = True
        mcu.burst_write_regs(0, [1, 2, 3])
        bulk = [op for op in tp.ops if op[0] == "bulk"]
        assert len(bulk) == 1
        # 4B TXINFO + 4B destination address + 3 words + 4B staging tail.
        assert bulk[0][1] == 4 + 4 + 12 + 4
        payload = tp.payloads[-1]
        assert int.from_bytes(payload[4:8], "little") == C.MT_MCU_MEMMAP_WLAN


class TestFirmwareRunning:
    def test_reports_true_when_com_reg0_is_one(self) -> None:
        assert MT7601UMcu(FakeTransport(com_reg0=[1])).firmware_running() is True

    def test_reports_false_for_any_other_value(self) -> None:
        assert MT7601UMcu(FakeTransport(com_reg0=[0])).firmware_running() is False
        assert MT7601UMcu(FakeTransport(com_reg0=[0xFFFFFFFF])).firmware_running() is False


def make_image(ilm: int = 64, dlm: int = 0) -> bytes:
    return (ilm.to_bytes(4, "little") + dlm.to_bytes(4, "little")
            + (0x7640).to_bytes(2, "little") + (0x0100).to_bytes(2, "little")
            + b"\x00" * 4 + b"201302052146" + b"\x00" * 4
            + b"\xaa" * (ilm + dlm))


class TestFirmwareHeader:
    def test_valid_image_passes(self) -> None:
        validate(make_image(ilm=1024, dlm=256))

    def test_short_image_rejected(self) -> None:
        with pytest.raises(FirmwareError, match="shorter than its header"):
            validate(b"\x00" * 8)

    def test_ilm_len_must_exceed_the_ivb(self) -> None:
        with pytest.raises(FirmwareError, match="implausible ILM length"):
            validate(make_image(ilm=64))

    def test_size_must_match_header(self) -> None:
        img = bytearray(make_image(ilm=1024, dlm=0))
        img += b"\x00"                     # one byte too many
        with pytest.raises(FirmwareError, match="header-implied"):
            validate(bytes(img))

    def test_describe_reads_version_and_build_time(self) -> None:
        text = describe(make_image(ilm=64))
        assert "Firmware Version: 0.1.00" in text
        assert "Build: 7640" in text
        assert "201302052146" in text

    def test_header_len_matches_the_c_struct(self) -> None:
        assert FW_HEADER_LEN == 32


class TestChunkSizing:
    def test_urb_payload_matches_the_real_image(self) -> None:
        # 45380-byte ILM minus the 64-byte IVB, chunked at 0x3800.
        assert MCU_FW_URB_MAX_PAYLOAD == 0x3800
        ilm = 45316
        sizes = [min(MCU_FW_URB_MAX_PAYLOAD, ilm - n)
                 for n in range(0, ilm, MCU_FW_URB_MAX_PAYLOAD)]
        assert sizes == [14336, 14336, 14336, 2308]
        # Each chunk is transferred as 4B descriptor + data + 4B staging.
        assert [s + 8 for s in sizes] == [14344, 14344, 14344, 2316]