"""Hardware-free regression for the M2b BB phydm walker.

The full byte-for-byte check vs the cold-boot capture is
`scripts/rtl8814au_dkms/verify_pcap.py`; this pins the walker's branch logic and
the chip-param `driver1` that selects taken rows.
"""
from wifit3.chips.rtl8814au_dkms import bb
from wifit3.chips.rtl8814au_dkms.bb_agc_tab_tbl import AGC_TAB
from wifit3.chips.rtl8814au_dkms.bb_phy_reg_tbl import PHY_REG


class Rec:
    """Records write32; the BB tables never read, so reads are unneeded here."""

    def __init__(self):
        self.writes = []

    def write32(self, a, v):
        self.writes.append((a, v))


def test_driver1_is_wire_confirmed_value():
    # cut A->0xF, package 0->0xF, interface USB=0x2, platform CE=0x8, rfe_type=1.
    assert bb._build_driver1(1) == 0x0F08F201


def test_check_positive_matches_only_rfe_for_bare_condition():
    d1 = bb._build_driver1(1)
    # A bare condition (only the rfe byte set) matches iff rfe_type == 1.
    assert bb._check_positive(d1, 0x80000001)       # BIT31 marker masked off
    assert not bb._check_positive(d1, 0x80000002)
    # Cut/package/interface nibbles are checked only when non-zero.
    assert bb._check_positive(d1, 0x0F00F201)       # all nibbles match
    assert not bb._check_positive(d1, 0x0100F201)   # wrong cut nibble


def test_walker_if_else_endif():
    d1 = bb._build_driver1(1)
    BIT31, BIT30 = 1 << 31, 1 << 30
    # The condition (rfe byte) lives in the IF word; the BIT30 word is the pair.
    # IF rfe==1 -> write A; ELSE -> write B; ENDIF -> write C (always).
    matched = (
        BIT31 | 0x01, 0x0,           # IF rfe==1 (selector 0)
        BIT30 | 0x00, 0x00,          # negative pair (marker only)
        0x100, 0xAAAA,               # taken (rfe matches 1)
        BIT31 | (2 << 28), 0x0,      # ELSE
        0x200, 0xBBBB,               # skipped
        BIT31 | (3 << 28), 0x0,      # ENDIF
        0x300, 0xCCCC,               # always
    )
    rec = Rec()
    bb._walk_table(rec, matched, d1)
    assert rec.writes == [(0x100, 0xAAAA), (0x300, 0xCCCC)]

    # Same shape but IF rfe==2 (no match) -> ELSE branch is taken instead.
    unmatched = (
        BIT31 | 0x02, 0x0,           # IF rfe==2 (does not match rfe 1)
        BIT30 | 0x00, 0x00,
        0x100, 0xAAAA,               # skipped
        BIT31 | (2 << 28), 0x0,      # ELSE
        0x200, 0xBBBB,               # taken
        BIT31 | (3 << 28), 0x0,      # ENDIF
        0x300, 0xCCCC,               # always
    )
    rec = Rec()
    bb._walk_table(rec, unmatched, d1)
    assert rec.writes == [(0x200, 0xBBBB), (0x300, 0xCCCC)]


def test_bb_tables_apply_2102_writes():
    """driver1 selects exactly the 2102 BB writes seen on the cold-boot wire."""
    d1 = bb._build_driver1(1)
    rec = Rec()
    bb._walk_table(rec, PHY_REG, d1)
    n_phy = len(rec.writes)
    bb._walk_table(rec, AGC_TAB, d1)
    n_total = len(rec.writes)
    assert n_phy > 0
    assert n_total - n_phy > 0
    assert n_total == 2102
