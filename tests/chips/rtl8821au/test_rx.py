"""Tests for the RX path: rx_pkt_desc parsing + multi-frame iteration."""
from __future__ import annotations

import struct

from wifit3.chips.rtl8821au.rx import (
    RX_PKT_DESC_SZ,
    iter_bulk_frames,
    parse_rx_pkt_desc,
)


def _make_rx_desc(
    *, pkt_len: int, drv_info_sz_8b: int = 0, shift: int = 0,
    is_c2h: bool = False, phy_status: bool = False,
) -> bytes:
    w0 = (
        (pkt_len & 0x3FFF)
        | ((drv_info_sz_8b & 0xF) << 16)
        | ((shift & 0x3) << 24)
        | ((1 << 26) if phy_status else 0)
    )
    w2 = (1 << 28) if is_c2h else 0
    return struct.pack("<6I", w0, 0, w2, 0, 0, 0)


def test_parse_simple_desc():
    desc = _make_rx_desc(pkt_len=128)
    stat = parse_rx_pkt_desc(desc)
    assert stat.pkt_len == 128
    assert stat.drv_info_sz == 0
    assert stat.shift == 0
    assert not stat.is_c2h
    assert stat.mpdu_offset == RX_PKT_DESC_SZ
    assert stat.total_size == RX_PKT_DESC_SZ + 128


def test_parse_with_drv_info_and_shift():
    """drv_info_sz field is in 8-byte units → stored as bytes."""
    desc = _make_rx_desc(pkt_len=100, drv_info_sz_8b=4, shift=2)
    stat = parse_rx_pkt_desc(desc)
    assert stat.drv_info_sz == 32      # 4 * 8
    assert stat.shift == 2
    assert stat.mpdu_offset == RX_PKT_DESC_SZ + 32 + 2
    assert stat.total_size == RX_PKT_DESC_SZ + 32 + 2 + 100


def test_parse_c2h_flag():
    desc = _make_rx_desc(pkt_len=10, is_c2h=True)
    stat = parse_rx_pkt_desc(desc)
    assert stat.is_c2h


def test_iter_skips_c2h():
    # Frame 1: C2H, len=10
    # Frame 2: real, len=20 (a fake beacon-ish blob)
    desc1 = _make_rx_desc(pkt_len=10, is_c2h=True)
    body1 = b"\x00" * 10
    pad1 = b"\x00" * (((RX_PKT_DESC_SZ + 10 + 7) & ~7) - (RX_PKT_DESC_SZ + 10))
    desc2 = _make_rx_desc(pkt_len=20)
    body2 = bytes(range(20))
    buf = desc1 + body1 + pad1 + desc2 + body2
    frames = list(iter_bulk_frames(buf))
    assert len(frames) == 1
    stat, mpdu, _ = frames[0]
    assert stat.pkt_len == 20
    assert mpdu == body2


def test_iter_stops_on_empty_desc():
    """A zero-pkt_len desc after good frames marks end-of-buffer."""
    desc1 = _make_rx_desc(pkt_len=16)
    body1 = b"A" * 16
    pad1 = b"\x00" * (((RX_PKT_DESC_SZ + 16 + 7) & ~7) - (RX_PKT_DESC_SZ + 16))
    zero_desc = _make_rx_desc(pkt_len=0)
    buf = desc1 + body1 + pad1 + zero_desc
    frames = list(iter_bulk_frames(buf))
    assert len(frames) == 1


def test_iter_handles_8byte_alignment():
    """pkt_len=15 → total=39 → next frame starts at 40."""
    desc1 = _make_rx_desc(pkt_len=15)
    body1 = b"B" * 15
    pad1 = b"\x00" * (40 - (RX_PKT_DESC_SZ + 15))
    desc2 = _make_rx_desc(pkt_len=8)
    body2 = b"C" * 8
    buf = desc1 + body1 + pad1 + desc2 + body2
    frames = list(iter_bulk_frames(buf))
    assert len(frames) == 2
    assert frames[0][1] == body1
    assert frames[1][1] == body2


def test_iter_truncated_returns_what_it_has():
    """A buffer cut off mid-frame should not raise — it just stops."""
    desc1 = _make_rx_desc(pkt_len=64)
    body1 = b"X" * 30     # < 64, truncated
    buf = desc1 + body1
    frames = list(iter_bulk_frames(buf))
    assert frames == []
