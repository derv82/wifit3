"""rtl8188ftv M7 RX acceptance: rxdesc24 decode, RSSI, bulk-frame walk.

Covers the values computed from kernel C (core.c:5721-5753, 6494;
8188f.c:1676-1706) independent of the capture: rate-aware CCK/OFDM RSSI,
the 8-byte round-up frame walk, and C2H/TX-report + HW-corrupt frame
filtering.  The live bulk-IN FIFO replay is gated in
scripts/chips/rtl8188ftv/verify_pcap.py (_rx_gate).
"""
import struct

import pytest

from wifit3.chips.rtl8188ftv.constants import (
    DESC_RATE_LAST_CCK,
    PHY_STATS_SZ_8188F,
    RX_FRAME_ALIGN_8188F,
    RX_PKT_DESC_SZ_8188F,
)
from wifit3.chips.rtl8188ftv.rx import iter_bulk_frames, parse_phystats_rssi, parse_rxdesc24


def _rx_buf(pkt_len: int = 20, *, rpt_sel: int = 0, crc_err: bool = False,
            icv_err: bool = False, rxmcs: int = 0, extra: bytes = b"") -> bytes:
    """Pack a minimal rxdesc24 + phy-stats + MPDU into a bulk-IN buffer."""
    w0 = pkt_len | (1 << 26)
    if crc_err:
        w0 |= 1 << 14
    if icv_err:
        w0 |= 1 << 15
    w2 = (rpt_sel & 0x1) << 28
    w3 = rxmcs & 0x7F
    desc = struct.pack("<6I", w0, 0, w2, w3, 0, 0)
    stats = bytes(PHY_STATS_SZ_8188F)
    return desc + stats + bytes(pkt_len) + extra


def test_parse_rxdesc24_decodes_fields():
    buf = _rx_buf(pkt_len=0x3FF, rxmcs=0x05)
    desc = parse_rxdesc24(buf)
    assert desc.pkt_len == 0x3FF
    assert desc.rxmcs == 0x05
    assert desc.phy_stats_present is True
    assert desc.rpt_sel == 0
    assert desc.crc_err is False
    assert desc.icv_err is False


def test_parse_rxdesc24_notes_corrupt_flags():
    buf = _rx_buf(pkt_len=16, crc_err=True, icv_err=True)
    desc = parse_rxdesc24(buf)
    assert desc.crc_err is True
    assert desc.icv_err is True


def test_parse_rxdesc24_rejects_truncated_header():
    with pytest.raises(ValueError):
        parse_rxdesc24(b"\x00" * (RX_PKT_DESC_SZ_8188F - 1))


def test_parse_phystats_rssi_ckk_uses_lna_vga_table():
    # LNA idx 1: -44 + 2*(19-vga).  agc_rpt = (1<<5) | 10 → vga=10 → -44+18=-26
    buf = bytes(PHY_STATS_SZ_8188F)
    buf = bytearray(buf)
    buf[5] = (1 << 5) | 10
    rssi = parse_phystats_rssi(bytes(buf), 0, rxmcs=DESC_RATE_LAST_CCK)
    assert rssi == -26


def test_parse_phystats_rssi_ofdm_pwdb_formula():
    # OFDM path: (pwdb >> 1) - 110.  pwdb=140 → 70-110 = -40
    buf = bytes(PHY_STATS_SZ_8188F)
    buf = bytearray(buf)
    buf[4] = 140
    rssi = parse_phystats_rssi(bytes(buf), 0, rxmcs=DESC_RATE_LAST_CCK + 1)
    assert rssi == -40


def test_parse_phystats_rssi_short_stats_is_none():
    assert parse_phystats_rssi(b"\x00" * 16, 0, rxmcs=0) is None


def test_iter_bulk_frames_walks_aligned_buffers():
    frame = _rx_buf(pkt_len=20)
    out = list(iter_bulk_frames(frame))
    assert len(out) == 1
    desc, mpdu, rssi = out[0]
    assert desc.pkt_len == 20
    assert len(mpdu) == 20


def test_iter_bulk_frames_rounds_positions_to_8_bytes():
    frame = _rx_buf(pkt_len=26)  # desc24 + stats32 = 56; +26 = 82 → roundup=88
    assert RX_FRAME_ALIGN_8188F == 8
    out = list(iter_bulk_frames(frame))
    assert len(out) == 1
    assert out[0][1]


def test_iter_bulk_frames_drops_c2h_and_tx_report():
    frame = _rx_buf(pkt_len=20, rpt_sel=1)
    assert list(iter_bulk_frames(frame)) == []


def test_iter_bulk_frames_drops_corrupt_frames():
    frame = _rx_buf(pkt_len=20, crc_err=True)
    assert list(iter_bulk_frames(frame)) == []
    frame2 = _rx_buf(pkt_len=20, icv_err=True)
    assert list(iter_bulk_frames(frame2)) == []