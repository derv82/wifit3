"""Tests for the EFUSE logical-map walker and rfe_option resolution.

Targets pure-Python logic only (no USB transport). Real hardware testing
of the read path lives in scripts/rtl8812au/test_hw_rtl8812au.py --phase efuse.
"""

from __future__ import annotations

from wifit3.chips.rtl8812au.efuse import (
    _classify_amplifier,
    _resolve_rfe_option,
    parse_logical_efuse_map,
)


class TestLogicalMapWalker:
    def test_all_0xff_physical_map_yields_all_0xff_logical(self):
        phy = bytes([0xFF] * 512)
        log = parse_logical_efuse_map(phy)
        assert all(b == 0xFF for b in log)
        assert len(log) == 512

    def test_one_byte_header_writes_one_word(self):
        """1-byte header format: blk_idx=2 (bits 7..4), word_en=0xE (write word 0 only)."""
        # log_idx for blk_idx=2 word_i=0: (2 << 3) + (0 << 1) = 16
        # hdr1 = (blk_idx << 4) | word_en = (2 << 4) | 0xE = 0x2E
        # Then 2 data bytes for word 0, then 0xFF terminator.
        phy = bytes([0x2E, 0xAA, 0xBB] + [0xFF] * 509)
        log = parse_logical_efuse_map(phy)
        assert log[16] == 0xAA
        assert log[17] == 0xBB
        # Everything else still 0xFF.
        assert log[0] == 0xFF
        assert log[15] == 0xFF
        assert log[18] == 0xFF

    def test_two_byte_header_blk_idx_25_word_1(self):
        """2-byte header format for blk_idx > 15 — used for the rfe_option offset 0xCA.

        Encode word_i=1 in blk_idx=25:
          hdr1 = (blk_idx[2:0] << 5) | 0x0F = (1 << 5) | 0x0F = 0x2F
          hdr2 = (blk_idx[6:3] << 4) | word_en = (3 << 4) | 0xD = 0x3D
          (word_en bit 1 = 0 means word 1 is written; others = 1 = skipped)
        log_idx = (25 << 3) + (1 << 1) = 202 = 0xCA  (= rfe_option offset)
        """
        phy = bytes([0x2F, 0x3D, 0xAA, 0xBB] + [0xFF] * 508)
        log = parse_logical_efuse_map(phy)
        assert log[0xCA] == 0xAA, f"got 0x{log[0xCA]:02x} at 0xCA"
        assert log[0xCB] == 0xBB
        assert log[0xC9] == 0xFF
        assert log[0xCC] == 0xFF

    def test_walker_handles_blank_eeprom(self):
        """A 2-byte header where hdr1[4:0]==0xF and hdr2==0xFF is "invalid" and stops the walk."""
        # First byte hdr1 = 0x1F means bit pattern 0001_1111. (hdr1 & 0x1F)==0xF triggers
        # 2-byte-header path, and if hdr2 == 0xFF the walker sees that as invalid and breaks.
        phy = bytes([0x1F, 0xFF] + [0x55] * 510)  # all the 0x55s should be untouched
        log = parse_logical_efuse_map(phy)
        # Should bail without writing anything.
        assert all(b == 0xFF for b in log)


class TestAmplifierClassification:
    """Mirrors rtw88xxa.c:32..78 rtw8812a_read_amplifier_type."""

    def test_ext_pa_2g_requires_bits_4_and_5(self):
        # bit 5 set, bit 4 set → ext_pa_2g = 1
        amp = _classify_amplifier(pa_type=0x30, lna_type_2g=0, lna_type_5g=0)
        assert amp["ext_pa_2g"] == 1

    def test_ext_pa_2g_only_one_bit_set_means_no_ext_pa(self):
        # bit 5 only → no ext_pa_2g (kernel requires BOTH bits)
        amp = _classify_amplifier(pa_type=0x20, lna_type_2g=0, lna_type_5g=0)
        assert amp["ext_pa_2g"] == 0
        # bit 4 only → likewise no
        amp = _classify_amplifier(pa_type=0x10, lna_type_2g=0, lna_type_5g=0)
        assert amp["ext_pa_2g"] == 0

    def test_ext_lna_2g_requires_bits_3_and_7(self):
        amp = _classify_amplifier(pa_type=0, lna_type_2g=0x88, lna_type_5g=0)
        assert amp["ext_lna_2g"] == 1
        # Only bit 7 → no
        amp = _classify_amplifier(pa_type=0, lna_type_2g=0x80, lna_type_5g=0)
        assert amp["ext_lna_2g"] == 0

    def test_5g_uses_different_bit_positions(self):
        # ext_pa_5g requires bit 0 AND bit 1
        amp = _classify_amplifier(pa_type=0x03, lna_type_2g=0, lna_type_5g=0)
        assert amp["ext_pa_5g"] == 1
        amp = _classify_amplifier(pa_type=0x02, lna_type_2g=0, lna_type_5g=0)
        assert amp["ext_pa_5g"] == 0
        # ext_lna_5g: same as 2g (bit 7 and bit 3)
        amp = _classify_amplifier(pa_type=0, lna_type_2g=0, lna_type_5g=0x88)
        assert amp["ext_lna_5g"] == 1


class TestRfeOptionResolution:
    """Mirrors rtw88xxa.c:80..122 rtw8812a_read_rfe_type."""

    def _ext(self, **overrides):
        base = {"ext_pa_2g": 0, "ext_lna_2g": 0, "ext_pa_5g": 0, "ext_lna_5g": 0}
        base.update(overrides)
        return base

    def test_efuse_unset_defaults_to_zero_on_usb(self):
        # Kernel: USB + map->rfe_option == 0xFF → efuse->rfe_option = 0
        assert _resolve_rfe_option(0xFF, self._ext()) == 0

    def test_bit_7_set_with_all_ext_flags_resolves_to_3(self):
        # Kernel: BIT(7) set + ext_lna_5g + ext_pa_5g + ext_lna_2g + ext_pa_2g → 3
        ext_all = self._ext(ext_pa_2g=1, ext_lna_2g=1, ext_pa_5g=1, ext_lna_5g=1)
        assert _resolve_rfe_option(0x83, ext_all) == 3

    def test_bit_7_set_with_only_5g_ext_resolves_to_zero(self):
        # ext_lna_5g + ext_pa_5g but no 2g → 0
        ext = self._ext(ext_lna_5g=1, ext_pa_5g=1)
        assert _resolve_rfe_option(0x80, ext) == 0

    def test_bit_7_set_with_only_lna_5g_resolves_to_2(self):
        ext = self._ext(ext_lna_5g=1)
        assert _resolve_rfe_option(0x80, ext) == 2

    def test_bit_7_set_with_no_lna_5g_resolves_to_4(self):
        assert _resolve_rfe_option(0x80, self._ext()) == 4

    def test_plain_rfe_option_passes_through(self):
        assert _resolve_rfe_option(0x03, self._ext()) == 3
        assert _resolve_rfe_option(0x02, self._ext()) == 2

    def test_rfe_4_with_ext_pa_forces_zero_workaround(self):
        # Kernel: workaround for bad EFUSE — rfe=4 + any ext_* on USB → 0
        ext = self._ext(ext_pa_2g=1)
        assert _resolve_rfe_option(0x04, ext) == 0

    def test_rfe_4_without_ext_flags_stays_4(self):
        assert _resolve_rfe_option(0x04, self._ext()) == 4
