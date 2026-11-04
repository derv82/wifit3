"""Hardware-free regression for update_txdesc (the monitor-injection TX descriptor).

Pins the SET_TX_DESC_*_8812 field positions, the rtl8812a_cal_txdesc_chksum XOR, the
monitor path's MACID / retry limit, and rtw_dump_xframe's packet-offset pull.
"""
import struct
from functools import reduce

import pytest

from wifit3.chips.rtl8821au_dkms import tx


def _field(desc, byte_off, bit_start, bit_len):
    word = int.from_bytes(desc[byte_off:byte_off + 4], "little")
    return (word >> bit_start) & ((1 << bit_len) - 1)


def _frame(length: int, addr1: bytes = bytes.fromhex("aabbccddeeff")) -> bytes:
    """A deauth-shaped MPDU of ``length`` bytes (FC 0xC0)."""
    head = b"\xc0\x00\x00\x00" + addr1
    return head + bytes(length - len(head))


# update_txdesc of a 26-byte unicast deauth, 1 Mbps CCK, RATE_ID B.
_GOLDEN_UNICAST_26 = bytes.fromhex(
    "1a00288c011208000000000000010000000032000000000000000000"
    "099f00000080000000000000"
)


def test_golden_unicast_descriptor():
    payload = tx.update_txdesc(_frame(26), rate_id=tx.RATEID_IDX_B, hw_rate=tx.DESC_RATE1M)
    assert payload[:tx.TXDESC_SIZE] == _GOLDEN_UNICAST_26


def test_fields():
    payload = tx.update_txdesc(_frame(64), rate_id=tx.RATEID_IDX_B, hw_rate=tx.DESC_RATE1M)
    d = payload[:tx.TXDESC_SIZE]
    assert _field(d, 0, 0, 16) == 64            # PKT_SIZE
    assert _field(d, 0, 16, 8) == 40            # OFFSET = TXDESC_SIZE
    assert _field(d, 0, 26, 1) == 1             # LAST_SEG
    assert _field(d, 0, 27, 1) == 1             # FIRST_SEG
    assert _field(d, 0, 31, 1) == 1             # OWN
    assert _field(d, 0, 24, 1) == 0             # BMC: unicast addr1
    assert _field(d, 4, 0, 7) == tx.RTW_DEFAULT_MGMT_MACID
    assert _field(d, 4, 8, 5) == tx.QSLT_MGNT
    assert _field(d, 4, 16, 5) == tx.RATEID_IDX_B
    assert _field(d, 4, 24, 5) == 0             # PKT_OFFSET: pulled
    assert _field(d, 12, 8, 1) == 1             # USE_RATE
    assert _field(d, 16, 0, 7) == tx.DESC_RATE1M
    assert _field(d, 16, 17, 1) == 1            # RETRY_LIMIT_ENABLE
    assert _field(d, 16, 18, 6) == tx.MGNT_DATA_RETRY_LIMIT
    assert _field(d, 32, 15, 1) == 1            # HWSEQ_EN
    assert payload[tx.TXDESC_SIZE:] == _frame(64)


def test_bmc_from_addr1_and_rate_params():
    frame = _frame(100, addr1=b"\xff" * 6)
    d = tx.update_txdesc(frame, rate_id=tx.RATEID_IDX_G, hw_rate=tx.DESC_RATE6M)
    assert _field(d, 0, 24, 1) == 1
    assert _field(d, 16, 0, 7) == tx.DESC_RATE6M
    assert _field(d, 4, 16, 5) == tx.RATEID_IDX_G


def test_bulk_size_boundary_keeps_the_packet_offset():
    pkt_len = tx.USB_HIGH_SPEED_BULK_SIZE - tx.TXDESC_SIZE
    frame = _frame(pkt_len)
    payload = tx.update_txdesc(frame, rate_id=tx.RATEID_IDX_B, hw_rate=tx.DESC_RATE1M)
    assert _field(payload, 4, 24, 5) == 1       # PKT_OFFSET, unit 8 bytes
    assert payload[tx.TXDESC_SIZE:tx.TXDESC_SIZE + tx.PACKET_OFFSET_SZ] == bytes(tx.PACKET_OFFSET_SZ)
    assert payload[tx.TXDESC_SIZE + tx.PACKET_OFFSET_SZ:] == frame


def test_checksum_validates():
    d = bytearray(tx.update_txdesc(_frame(123), rate_id=tx.RATEID_IDX_G, hw_rate=tx.DESC_RATE6M)[:tx.TXDESC_SIZE])
    stored = int.from_bytes(d[28:30], "little")
    d[28:30] = b"\x00\x00"
    assert tx.txdesc_checksum(d) == stored


def test_checksum_invariant_xor_zero():
    for pkt_len in (26, 100, 472, 1500):
        d = tx.update_txdesc(_frame(pkt_len), rate_id=tx.RATEID_IDX_B, hw_rate=tx.DESC_RATE1M)
        assert reduce(lambda a, x: a ^ x, struct.unpack("<16H", d[:32]), 0) == 0


def test_rejects_pkt_len_outside_descriptor_field():
    with pytest.raises(ValueError, match="pkt_len"):
        tx.update_txdesc(_frame(0x10000), rate_id=tx.RATEID_IDX_B, hw_rate=tx.DESC_RATE1M)
