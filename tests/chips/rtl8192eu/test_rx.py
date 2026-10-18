"""RX path: rtl8xxxu_rxdesc24 decode, phy-stats RSSI, 8-byte URB iteration."""
from __future__ import annotations

import struct

import pytest

from wifit3.chips.rtl8192eu.rx import (
    RX_FRAME_ALIGN_24,
    RX_PKT_DESC_SZ_24,
    iter_bulk_frames,
    parse_phystats_rssi,
    parse_rxdesc24,
)


def rxdesc(
    *,
    pkt_len: int = 0,
    crc_err: bool = False,
    icv_err: bool = False,
    drv_units: int = 0,
    shift: int = 0,
    phy_stats: bool = False,
    rpt_sel: int = 0,
    usb_agg_pktnum: int = 0,
    rxmcs: int = 0,
) -> bytes:
    """Build a 24-byte rxdesc24 from the rtl8xxxu.h bitfields.

    w0 = pktlen:14 | crc32:1 | icverr:1 | drvinfo_sz:4 | security:3 | qos:1 |
         shift:2 | phy_stats:1 | swdec:1 | ls:1 | fs:1 | eor:1 | own:1
    w2 bit 28 = rpt_sel; w3 = rxmcs:7 | ... | usb_agg_pktnum:8 | ...
    """
    w0 = pkt_len & 0x3FFF
    w0 |= (1 << 14) if crc_err else 0
    w0 |= (1 << 15) if icv_err else 0
    w0 |= (drv_units & 0xF) << 16
    w0 |= (shift & 0x3) << 24
    w0 |= (1 << 26) if phy_stats else 0
    w2 = (rpt_sel & 0x1) << 28
    w3 = ((usb_agg_pktnum & 0xFF) << 16) | (rxmcs & 0x7F)
    # w1, w4 and the trailing ``u32 tsfl`` are unused by this RX path.
    return struct.pack("<6I", w0, 0, w2, w3, 0, 0)


def frame(
    mpdu: bytes,
    *,
    drv_units: int = 0,
    shift: int = 0,
    rxmcs: int = 0,
    rpt_sel: int = 0,
    phy_stats: bool = False,
    stats: bytes = b"",
) -> bytes:
    """One rxdesc24 + phy-stats + shift-pad + MPDU slice (no inter-frame padding)."""
    assert len(stats) == drv_units * 8
    return (
        rxdesc(
            pkt_len=len(mpdu), drv_units=drv_units, shift=shift,
            phy_stats=phy_stats, rpt_sel=rpt_sel, rxmcs=rxmcs,
        )
        + stats
        + b"\x00" * shift
        + mpdu
    )


# ---- descriptor decode -------------------------------------------------


def test_rxdesc24_is_24_bytes():
    assert RX_PKT_DESC_SZ_24 == 24
    assert RX_FRAME_ALIGN_24 == 8


def test_parse_hand_built_descriptor():
    buf = rxdesc(
        pkt_len=42, crc_err=True, icv_err=True, drv_units=4, shift=2,
        phy_stats=True, rpt_sel=1, usb_agg_pktnum=3, rxmcs=0x4B,
    )
    desc = parse_rxdesc24(buf)
    assert desc.pkt_len == 42
    assert desc.crc_err is True
    assert desc.icv_err is True
    assert desc.drv_info_sz_bytes == 32          # 4 × 8
    assert desc.shift == 2
    assert desc.phy_stats_present is True
    assert desc.rpt_sel == 1
    assert desc.usb_agg_pktnum == 3
    assert desc.rxmcs == 0x4B
    # 24-byte header + 32-byte phy stats + 2 pad bytes.
    assert desc.mpdu_offset == 58
    assert desc.total_size == 100


def test_parse_at_nonzero_offset():
    buf = b"\xAA" * 7 + rxdesc(pkt_len=7)
    assert parse_rxdesc24(buf, offset=7).pkt_len == 7


def test_parse_short_buffer_raises():
    with pytest.raises(ValueError, match="rxdesc24 needs 24 bytes"):
        parse_rxdesc24(b"\x00" * 23)


def test_rxmcs_is_seven_bits():
    assert parse_rxdesc24(rxdesc(rxmcs=0xFF)).rxmcs == 0x7F


# ---- RSSI --------------------------------------------------------------


def test_ofdm_rssi_is_pwdb_over_two_minus_110():
    stats = bytearray(32)
    stats[4] = 0x80                                # cck_sig_qual_ofdm_pwdb_all
    assert parse_phystats_rssi(bytes(stats), 0, rxmcs=0x0B) == (0x80 >> 1) - 110


def test_cck_rssi_uses_the_8192e_lna_vga_table():
    stats = bytearray(32)
    stats[5] = (3 << 5) | 5                        # LNA 3, VGA 5
    # Table 0 index 3 = -21, then -2 dB per VGA step.
    assert parse_phystats_rssi(bytes(stats), 0, rxmcs=0x00) == -31
    # Report type 1 selects the other table: index 3 = -4.
    assert parse_phystats_rssi(
        bytes(stats), 0, rxmcs=0x00, cck_agc_report_type=1
    ) == -14


def test_phy_stats_too_short_returns_none():
    assert parse_phystats_rssi(b"\x00" * 31, 0, rxmcs=0x0B) is None


# ---- bulk-URB iteration -------------------------------------------------


def test_iter_yields_each_frame_and_rounds_up_to_8_bytes():
    f1 = frame(b"abcde")                           # total 29 -> next at 32
    f2 = frame(b"12345678")                        # total 32
    buf = f1 + b"\x00" * 3 + f2

    parsed = list(iter_bulk_frames(buf))
    assert [(desc.pkt_len, mpdu) for desc, mpdu, _ in parsed] == [
        (5, b"abcde"),
        (8, b"12345678"),
    ]
    assert parsed[0][0].mpdu_offset == 24


def test_iter_reads_rssi_from_the_phy_stats_block():
    stats = bytearray(32)
    stats[4] = 0x80
    buf = frame(b"beacon!", drv_units=4, phy_stats=True, stats=bytes(stats), rxmcs=0x0B)
    (desc, mpdu, rssi), = list(iter_bulk_frames(buf))
    assert mpdu == b"beacon!"
    assert desc.phy_stats_present
    assert rssi == -46


def test_iter_skips_tx_report_frames():
    c2h = frame(b"report", rpt_sel=1)
    real = frame(b"beacon")
    buf = c2h + b"\x00" * ((-len(c2h)) % 8) + real
    assert [mpdu for _, mpdu, _ in iter_bulk_frames(buf)] == [b"beacon"]


def test_iter_drops_hw_flagged_crc_and_icv_errors():
    bad_crc = rxdesc(pkt_len=5, crc_err=True) + b"bad!!"
    bad_crc += b"\x00" * ((-len(bad_crc)) % 8)
    bad_icv = rxdesc(pkt_len=5, icv_err=True) + b"bad!!"
    bad_icv += b"\x00" * ((-len(bad_icv)) % 8)
    good = frame(b"good!")
    buf = bad_crc + bad_icv + good
    assert [mpdu for _, mpdu, _ in iter_bulk_frames(buf)] == [b"good!"]


def test_iter_skips_zero_length_packet_and_continues():
    first = frame(b"first")
    zero = rxdesc(pkt_len=0)
    later = frame(b"later")
    buf = first + b"\x00" * ((-len(first)) % 8) + zero + later
    assert [mpdu for _, mpdu, _ in iter_bulk_frames(buf)] == [b"first", b"later"]


def test_iter_skips_a_trailing_zero_length_descriptor():
    good = frame(b"aaaa")
    buf = good + b"\x00" * ((-len(good)) % 8) + rxdesc(pkt_len=0)
    assert [mpdu for _, mpdu, _ in iter_bulk_frames(buf)] == [b"aaaa"]


def test_iter_stops_on_truncated_frame():
    buf = frame(b"0123456789")[:-4]
    assert list(iter_bulk_frames(buf)) == []


def test_iter_handles_empty_buffer():
    assert list(iter_bulk_frames(b"")) == []
