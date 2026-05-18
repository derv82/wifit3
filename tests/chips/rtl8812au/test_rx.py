"""Tests for the rate-aware Jaguar phy_status RSSI parser.

Synthetic phy_status buffers are constructed and fed through
:func:`parse_jaguar_phy_status_rssi` with known RxPktStat values to
verify the CCK vs OFDM branches.
"""

from __future__ import annotations

import struct

from wifit3.chips.rtl8812au.rx import (
    _rtw8812a_cck_rx_pwr,
    parse_jaguar_phy_status_rssi,
)
from wifit3.chips.rtw88_base.rx_common import RxPktStat


def _make_phy_status(w0: int = 0, w1: int = 0) -> bytes:
    """8-byte buffer with the given w0 + w1 (LE)."""
    return struct.pack("<II", w0, w1)


def _stat(rate: int) -> RxPktStat:
    return RxPktStat(
        pkt_len=200,
        crc_err=False,
        icv_err=False,
        drv_info_sz=32,
        shift=0,
        phy_status_present=True,
        is_c2h=False,
        rate=rate,
        bw=0,
        tsf_low=0,
        macid=0,
        ppdu_cnt=0,
    )


class TestCckRxPwrLookup:
    """Direct unit tests for the rtw8812a_cck_rx_pwr formula
    (rtw8812a.c:19..56)."""

    def test_lna_idx_7_high_signal(self):
        # vga_idx <= 27: rx_pwr = -94 + 2*(27 - vga_idx)
        assert _rtw8812a_cck_rx_pwr(7, 27) == -94
        assert _rtw8812a_cck_rx_pwr(7, 10) == -94 + 2 * 17   # = -60

    def test_lna_idx_7_low_signal(self):
        # vga_idx > 27: rx_pwr = -94
        assert _rtw8812a_cck_rx_pwr(7, 31) == -94

    def test_each_lna_branch(self):
        assert _rtw8812a_cck_rx_pwr(6, 0) == -42 + 2 * 2     # = -38
        assert _rtw8812a_cck_rx_pwr(5, 7) == -36             # 7-7=0
        assert _rtw8812a_cck_rx_pwr(4, 7) == -30
        assert _rtw8812a_cck_rx_pwr(3, 7) == -18
        assert _rtw8812a_cck_rx_pwr(2, 5) == 0
        assert _rtw8812a_cck_rx_pwr(1, 0) == 14
        assert _rtw8812a_cck_rx_pwr(0, 0) == 20

    def test_invalid_lna_idx_returns_zero(self):
        # >=8 hits default branch
        assert _rtw8812a_cck_rx_pwr(8, 0) == 0


class TestCckPath:
    """The parser should pick the CCK branch when rate <= DESC_RATE11M (0x03)."""

    def test_cck_extracts_lna_and_vga_from_w1(self):
        # vga_idx = w1[12:8], lna_idx = w1[15:13]
        # Encode lna_idx=7 (0b111), vga_idx=10 (0b01010)
        # → w1 = (7 << 13) | (10 << 8) = 0xE000 | 0x0A00 = 0xEA00
        buf = _make_phy_status(w0=0, w1=0xEA00)
        # For lna_idx=7, vga_idx=10: -94 + 2*(27-10) = -94 + 34 = -60
        rssi = parse_jaguar_phy_status_rssi(buf, 0, _stat(rate=0x00))
        assert rssi == -60

    def test_cck_at_all_descrate_cck_values(self):
        # All DESC_RATE0..3 should use CCK branch
        buf = _make_phy_status(w0=0, w1=0xEA00)   # lna=7 vga=10 → -60
        for cck_rate in (0x00, 0x01, 0x02, 0x03):
            assert parse_jaguar_phy_status_rssi(buf, 0, _stat(rate=cck_rate)) == -60


class TestOfdmPath:
    """The parser should pick the OFDM branch when rate > DESC_RATE11M."""

    def test_ofdm_extracts_gain_a_and_b_from_w0(self):
        # gain_a = w0[6:0], gain_b = w0[14:8]
        # Encode gain_a=80 (0x50), gain_b=50 (0x32)
        # → w0 = 80 | (50 << 8) = 0x3250
        buf = _make_phy_status(w0=0x3250, w1=0)
        # max(80-110, 50-110) = max(-30, -60) = -30
        rssi = parse_jaguar_phy_status_rssi(buf, 0, _stat(rate=0x06))  # DESC_RATE12M
        assert rssi == -30

    def test_ofdm_floor_when_both_paths_zero(self):
        buf = _make_phy_status(w0=0, w1=0)
        rssi = parse_jaguar_phy_status_rssi(buf, 0, _stat(rate=0x04))  # DESC_RATE6M
        assert rssi == -110

    def test_ofdm_at_descrate_ht_vht(self):
        # HT/VHT rates (anything beyond OFDM 6/9/.../54M) also OFDM-path
        buf = _make_phy_status(w0=0x3250, w1=0)
        for rate in (0x04, 0x10, 0x20, 0x80):
            assert parse_jaguar_phy_status_rssi(buf, 0, _stat(rate=rate)) == -30


class TestEdgeCases:
    def test_short_buffer_returns_none(self):
        # Need at least 8 bytes from offset
        assert parse_jaguar_phy_status_rssi(b"\x00" * 7, 0, _stat(rate=0)) is None

    def test_offset_skips_descriptor(self):
        # Buffer with leading padding; phy_status is at offset 24
        prefix = b"\xFF" * 24
        phy_status = _make_phy_status(w0=0x3250, w1=0)
        buf = prefix + phy_status
        rssi = parse_jaguar_phy_status_rssi(buf, 24, _stat(rate=0x06))
        assert rssi == -30
