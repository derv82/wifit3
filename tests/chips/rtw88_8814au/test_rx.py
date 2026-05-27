"""Tests for the RTL8814AU jaguar phy_status RSSI decode (pure logic).

OFDM = 2nd-lowest of the 4 per-path gains − 110; CCK = AGC LNA/VGA lookup.
"""
from __future__ import annotations

import struct
from types import SimpleNamespace

from wifit3.chips.rtw88_base.registers import DESC_RATE1M, DESC_RATE6M
from wifit3.chips.rtw88_8814au.rx import _cck_rx_pwr, parse_phy_status_rssi_8814a


def _ofdm_buf(g_a, g_b, g_c, g_d):
    """28-byte jaguar report: gains a/b in w0[6:0]/[14:8], c in w5[30:24], d in w6[6:0]."""
    w0 = (g_a & 0x7F) | ((g_b & 0x7F) << 8)
    w5 = (g_c & 0x7F) << 24
    w6 = g_d & 0x7F
    buf = bytearray(28)
    struct.pack_into("<I", buf, 0, w0)
    struct.pack_into("<I", buf, 20, w5)
    struct.pack_into("<I", buf, 24, w6)
    return bytes(buf)


class TestOfdmRssi:
    def test_second_lowest_gain_minus_110(self):
        # gains 70,72,68,74 -> 2nd-lowest = 70 -> 70-110 = -40 dBm.
        buf = _ofdm_buf(70, 72, 68, 74)
        stat = SimpleNamespace(rate=DESC_RATE6M)
        assert parse_phy_status_rssi_8814a(buf, 0, stat) == -40

    def test_power_save_outlier_rejected(self):
        # One path reports an absurd gain (power-save quirk); the 2nd-lowest trick
        # ignores it. gains 64,106,66,72: middle1=max(64,66)=66, middle2=min(106,
        # 72)=72 -> min=66 -> 66-110 = -44.
        buf = _ofdm_buf(64, 106, 66, 72)
        stat = SimpleNamespace(rate=DESC_RATE6M)
        assert parse_phy_status_rssi_8814a(buf, 0, stat) == -44

    def test_clamped_to_valid_range(self):
        buf = _ofdm_buf(0, 0, 0, 0)            # 0-110 = -110, clamped to floor
        stat = SimpleNamespace(rate=DESC_RATE6M)
        assert parse_phy_status_rssi_8814a(buf, 0, stat) == -110

    def test_too_short_returns_none(self):
        stat = SimpleNamespace(rate=DESC_RATE6M)
        assert parse_phy_status_rssi_8814a(b"\x00" * 20, 0, stat) is None


class TestCckRssi:
    def test_cck_lna_vga_lookup(self):
        # CCK: lna=5, vga=10 -> -28 - 2*10 = -48 dBm. w1[12:8]=vga, w1[15:13]=lna.
        w1 = (10 << 8) | (5 << 13)
        buf = bytearray(28)
        struct.pack_into("<I", buf, 4, w1)
        stat = SimpleNamespace(rate=DESC_RATE1M)
        assert parse_phy_status_rssi_8814a(bytes(buf), 0, stat) == -48

    def test_cck_rx_pwr_table(self):
        assert _cck_rx_pwr(7, 0) == -38
        assert _cck_rx_pwr(5, 10) == -48
        assert _cck_rx_pwr(2, 0) == -1
        assert _cck_rx_pwr(0, 5) == -10      # lna not in table -> base 0, -2*vga
