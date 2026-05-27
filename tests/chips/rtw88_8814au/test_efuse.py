"""Tests for the RTL8814AU EFUSE pure-logic: signed-nibble helper, the
TX-power-by-rate bitfield parse (the riskiest new code — 42 B/path with packed
signed s8:4 diffs), the word-enable logical-map walker, and rfe_option resolve.

Pure Python only (no USB transport). Real read-path testing lives in
scripts/rtw88_8814au/test_hw_8814au.py --phase efuse.
"""
from __future__ import annotations

from wifit3.chips.rtw88_8814au.efuse import (
    _parse_txpwr_path,
    _resolve_rfe_option,
    _s4,
    parse_logical_efuse_map,
)


class TestSignedNibble:
    def test_s4_sign_extension(self):
        assert _s4(0x0) == 0
        assert _s4(0x7) == 7        # largest positive
        assert _s4(0x8) == -8       # smallest negative
        assert _s4(0xF) == -1       # -1
        assert _s4(0xF5) == 5       # masks to the low nibble first
        assert _s4(0x35) == 5       # high nibble ignored


# One fully-populated 42-byte rtw_txpwr_idx block with hand-computed expectations.
# Layout: 2G = cck_base[6], bw40_base[5], ht_1s(1B: ofdm|bw20),
# ht_2s/3s/4s(2B: bw20|bw40, cck|ofdm); 5G = bw40_base[14], ht_1s(1B),
# ht_2s/3s/4s(1B: bw20|bw40), ofdm_diff(2B), vht_1s/2s/3s/4s(1B: bw160|bw80).
_BLOCK = bytes(
    [0x20, 0x21, 0x22, 0x23, 0x24, 0x25]        # cck_base
    + [0x26, 0x27, 0x28, 0x29, 0x2A]            # bw40_base_2g
    + [0x31]                                     # ht_1s_2g: ofdm=1, bw20=3
    + [0x12, 0x34]                               # ht_2s_2g: bw20=2,bw40=1,cck=4,ofdm=3
    + [0x00, 0x00]                               # ht_3s_2g: all 0
    + [0xF8, 0x00]                               # ht_4s_2g: bw20=-8, bw40=-1, cck=0, ofdm=0
    + list(range(0x30, 0x3E))                    # bw40_base_5g[14]
    + [0x21]                                     # ht_1s_5g: ofdm=1, bw20=2
    + [0x10, 0x00, 0x00]                         # ht_2s/3s/4s_5g: 2s bw40=1, rest 0
    + [0x21, 0x03]                               # ofdm_diff: 2s=2, 3s=1, 4s=3
    + [0x21, 0x00, 0x00, 0xF0]                   # vht 1s..4s: 1s bw80=2/bw160=1; 4s bw80=-1
)


class TestTxPowerParse:
    def setup_method(self):
        assert len(_BLOCK) == 42
        self.t = _parse_txpwr_path(_BLOCK, 0)

    def test_base_powers_are_raw_bytes(self):
        assert self.t.cck_base == (0x20, 0x21, 0x22, 0x23, 0x24, 0x25)
        assert self.t.bw40_base_2g == (0x26, 0x27, 0x28, 0x29, 0x2A)
        assert self.t.bw40_base_5g == tuple(range(0x30, 0x3E))

    def test_2g_diffs_signed(self):
        assert self.t.ht_1s_diff_2g == (1, 3)            # (ofdm, bw20)
        assert self.t.ht_ns_diff_2g == (
            (2, 1, 4, 3),                                # 2s (bw20,bw40,cck,ofdm)
            (0, 0, 0, 0),                                # 3s
            (-8, -1, 0, 0),                              # 4s — sign-extended
        )

    def test_5g_diffs_signed(self):
        assert self.t.ht_1s_diff_5g == (1, 2)            # (ofdm, bw20)
        assert self.t.ht_ns_diff_5g == ((0, 1), (0, 0), (0, 0))  # (bw20, bw40)
        assert self.t.ofdm_diff_5g == (2, 1, 3)          # (2s, 3s, 4s)
        assert self.t.vht_diff_5g == ((2, 1), (0, 0), (0, 0), (-1, 0))  # (bw80, bw160)

    def test_base_offset_is_honoured(self):
        padded = bytes([0xEE] * 10) + _BLOCK
        assert _parse_txpwr_path(padded, 10) == self.t


class TestLogicalMapWalker:
    def test_all_ff_yields_all_ff(self):
        log = parse_logical_efuse_map(bytes([0xFF] * 1024))
        assert len(log) == 512 and all(b == 0xFF for b in log)

    def test_one_byte_header_writes_one_word(self):
        # 1-byte hdr: blk_idx=2 (bits 7:4), word_en=0xE (only word 0 enabled).
        # log_idx = (2<<3)+(0<<1) = 16.
        phy = bytes([0x2E, 0xAA, 0xBB] + [0xFF] * 1021)
        log = parse_logical_efuse_map(phy)
        assert log[16] == 0xAA and log[17] == 0xBB
        assert log[15] == 0xFF and log[18] == 0xFF


class TestRfeOption:
    def test_bit7_set_maps_to_usb_1(self):
        assert _resolve_rfe_option(0x80) == 1
        assert _resolve_rfe_option(0x81) == 1

    def test_bit7_clear_is_raw(self):
        assert _resolve_rfe_option(0x00) == 0
        assert _resolve_rfe_option(0x02) == 2
