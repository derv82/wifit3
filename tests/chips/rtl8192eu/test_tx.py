"""TX path: txdesc40 MGMT descriptor, XOR-16 checksum, deauth builder, bulk-OUT."""
from __future__ import annotations

import struct
from unittest.mock import MagicMock

import pytest

from wifit3.chips.rtl8192eu.constants import (
    REASON_CODE_CLASS3_FRAME,
    TXDESC_QUEUE_MGNT,
    TXDESC_QUEUE_SHIFT,
)
from wifit3.chips.rtl8192eu.tx import (
    TXDESC40_RETRY_LIMIT_MGNT,
    TX_DESC_SZ_40,
    build_deauth,
    build_tx_desc_mgmt,
    calc_tx_desc_csum,
    pick_bulk_out_mgmt,
    send_mgmt_frame,
)

BSSID = bytes.fromhex("c006c3c9ec0b")
CLIENT = bytes.fromhex("d03f27223eef")


def word(desc: bytes, index: int) -> int:
    return struct.unpack_from("<I", desc, index * 4)[0]


# ---- descriptor ---------------------------------------------------------


def test_descriptor_is_40_bytes_with_offset_40():
    desc = build_tx_desc_mgmt(26, is_broadcast=False)
    assert len(desc) == TX_DESC_SZ_40 == 40
    assert struct.unpack_from("<H", desc, 0)[0] == 26   # pkt_size
    assert desc[2] == 40                                # pkt_offset
    assert desc[3] == 0x8C                              # OWN | FIRST | LAST


def test_broadcast_sets_bmc_in_txdw0():
    unicast = build_tx_desc_mgmt(26, is_broadcast=False)
    broadcast = build_tx_desc_mgmt(26, is_broadcast=True)
    assert unicast[3] == 0x8C
    assert broadcast[3] == 0x8D


def test_txdw1_routes_the_mgmt_queue():
    desc = build_tx_desc_mgmt(26, is_broadcast=False)
    assert word(desc, 1) == TXDESC_QUEUE_MGNT << TXDESC_QUEUE_SHIFT
    assert word(desc, 1) == 0x1200


def test_txdw2_is_agg_break():
    assert word(build_tx_desc_mgmt(26, is_broadcast=False), 2) == 1 << 16


def test_txdw3_uses_driver_rate_for_mgmt():
    assert word(build_tx_desc_mgmt(26, is_broadcast=False), 3) == 1 << 8


def test_txdw4_carries_the_retry_limit_and_enable():
    desc = build_tx_desc_mgmt(26, is_broadcast=False)
    expected = (TXDESC40_RETRY_LIMIT_MGNT << 18) | (1 << 17)
    assert word(desc, 4) == expected == 0x001A0000

    custom = build_tx_desc_mgmt(26, is_broadcast=False, retry_limit=10)
    assert word(custom, 4) == (10 << 18) | (1 << 17)


def test_txdw5_through_txdw9_are_zero_on_the_raw_inject_path():
    desc = build_tx_desc_mgmt(26, is_broadcast=False)
    for index in range(5, 10):
        assert word(desc, index) == 0, f"txdw{index} should be 0"


# ---- checksum -----------------------------------------------------------


def test_checksum_is_xor16_over_the_first_32_bytes():
    desc = build_tx_desc_mgmt(26, is_broadcast=False)
    expected = 0
    for i in range(0, 32, 2):
        expected ^= int.from_bytes(desc[i : i + 2], "little")

    calc_tx_desc_csum(desc)
    assert struct.unpack_from("<H", desc, 28)[0] == expected & 0xFFFF


def test_checksum_ignores_bytes_after_the_first_32():
    desc = build_tx_desc_mgmt(26, is_broadcast=True)
    calc_tx_desc_csum(desc)
    before = bytes(desc)

    desc[32:40] = b"\xFF" * 8
    calc_tx_desc_csum(desc)
    assert desc[28:30] == before[28:30]


def test_checksum_zeroes_a_stale_csum_field_first():
    desc = build_tx_desc_mgmt(26, is_broadcast=False)
    desc[28:30] = b"\xDE\xAD"
    calc_tx_desc_csum(desc)
    assert desc[28:30] != b"\xDE\xAD"


# ---- deauth builder -----------------------------------------------------


def test_build_deauth_layout():
    frame = build_deauth(BSSID, CLIENT, reason=REASON_CODE_CLASS3_FRAME)
    assert len(frame) == 26
    assert frame[0] == 0xC0                   # MGMT | deauth
    assert frame[1] == 0x00                   # no ToDS/FromDS/flags
    assert frame[2:4] == b"\x3A\x01"           # duration 0x013A
    assert frame[4:10] == CLIENT               # addr1 = destination
    assert frame[10:16] == BSSID               # addr2 = source (spoofed AP)
    assert frame[16:22] == BSSID               # addr3 = BSSID
    assert frame[22:24] == b"\x00\x00"         # seq_ctrl
    assert frame[24:26] == b"\x07\x00"         # reason 7, little-endian


def test_build_deauth_rejects_bad_addresses():
    with pytest.raises(ValueError, match="bssid"):
        build_deauth(b"\x00" * 5, CLIENT)
    with pytest.raises(ValueError, match="client"):
        build_deauth(BSSID, b"\x00" * 7)


# ---- endpoint selection + bulk-OUT -------------------------------------


def test_pick_bulk_out_mgmt_is_the_lowest_endpoint():
    assert pick_bulk_out_mgmt([0x03, 0x02]) == 0x02
    assert pick_bulk_out_mgmt([0x05, 0x06]) == 0x05
    with pytest.raises(RuntimeError, match="no bulk-OUT"):
        pick_bulk_out_mgmt([])


def test_send_mgmt_frame_prefixes_the_40_byte_descriptor():
    dev = MagicMock()
    dev.write.side_effect = lambda ep, data, timeout: len(data)
    frame = build_deauth(BSSID, CLIENT)

    written = send_mgmt_frame(dev, 0x02, frame)

    assert written == 66
    ep, urb, _timeout = dev.write.call_args.args
    assert ep == 0x02
    assert len(urb) == 40 + 26
    assert urb[40:] == frame
    assert urb[2] == 40


def test_send_mgmt_frame_raises_on_short_write():
    dev = MagicMock()
    dev.write.return_value = 10
    with pytest.raises(IOError, match="short write"):
        send_mgmt_frame(dev, 0x02, build_deauth(BSSID, CLIENT))
