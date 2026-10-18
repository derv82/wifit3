"""Firmware path: blob identity, 4096-byte pages over 128-byte vendor blocks, reset."""
from __future__ import annotations

import struct

from wifit3.chips.rtl8192eu.constants import (
    FW_HEADER_SIZE,
    FW_SIGNATURE_92E,
    FW_WRITE_BLOCK_SIZE,
    REG_FW_START_ADDRESS,
    REG_MCU_FW_DL,
    REG_RSV_CTRL,
    REG_SYS_FUNC,
    RTL_FW_PAGE_SIZE,
    SYS_FUNC_CPU_ENABLE,
)
from wifit3.chips.rtl8192eu.firmware import (
    _writeN,
    download_firmware,
    load_firmware_blob,
    reset_8051,
)

from tests.chips.rtl8192eu._mock import MockTransport


def test_block_size_is_128_and_pages_are_4096():
    assert FW_WRITE_BLOCK_SIZE == 128
    assert RTL_FW_PAGE_SIZE == 4096
    assert FW_HEADER_SIZE == 32


def test_shipped_blob_is_the_8192eu_image():
    blob = load_firmware_blob()
    signature, = struct.unpack_from("<H", blob, 0)
    assert len(blob) == 32286
    assert signature & 0xFFF0 == FW_SIGNATURE_92E


def test_writeN_chunks_on_128_byte_boundaries():
    t = MockTransport()
    payload = bytes(range(256)) + bytes(44)      # 300 bytes

    _writeN(t, REG_FW_START_ADDRESS, payload)

    blocks = [w for w in t.writes if w[0] == "block"]
    assert [(addr, len(data)) for _op, addr, data in blocks] == [
        (REG_FW_START_ADDRESS, 128),
        (REG_FW_START_ADDRESS + 128, 128),
        (REG_FW_START_ADDRESS + 256, 44),
    ]
    assert b"".join(data for _op, _addr, data in blocks) == payload


def test_download_uploads_headerless_payload_as_full_pages_plus_remainder():
    t = MockTransport()
    blob = load_firmware_blob()
    payload_len = len(blob) - FW_HEADER_SIZE

    download_firmware(t, blob)

    blocks = [w for w in t.writes if w[0] == "block"]
    assert sum(len(data) for _op, _addr, data in blocks) == payload_len
    # 7 full 4096-B pages (32×128-B blocks each) + a 3582-B remainder
    # (27×128-B blocks + a 126-B tail) => ceil(32254 / 128) = 252 blocks.
    assert len(blocks) == (payload_len + FW_WRITE_BLOCK_SIZE - 1) // FW_WRITE_BLOCK_SIZE
    assert len(blocks) == 252
    # Page selector advances 0..7; the last page carries the 3582-byte remainder.
    pages = [value for _op, _addr, value in t.writes_to(REG_MCU_FW_DL + 2)]
    assert pages == list(range(8))
    assert len(blocks[-1][2]) == payload_len % FW_WRITE_BLOCK_SIZE == 126


def test_download_disables_fw_download_in_the_finally_block():
    t = MockTransport()
    download_firmware(t, load_firmware_blob())
    # The final 16-bit write clears MCU_FW_DL_ENABLE (BIT(0)).
    assert t.writes_to(REG_MCU_FW_DL)[-1][0] == "w16"
    assert t.writes_to(REG_MCU_FW_DL)[-1][2] & 1 == 0


def test_reset_8051_toggles_rsv_ctrl_around_cpu_enable():
    t = MockTransport()
    t.seed(REG_RSV_CTRL + 1, 0xFF, 1)
    t.seed(REG_SYS_FUNC, 0x0000, 2)

    reset_8051(t)

    assert [v for _op, _addr, v in t.writes_to(REG_RSV_CTRL + 1)] == [0xFE, 0xFF]
    assert [v for _op, _addr, v in t.writes_to(REG_SYS_FUNC)] == [
        0x0000, SYS_FUNC_CPU_ENABLE,
    ]
