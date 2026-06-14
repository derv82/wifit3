"""Tests for the rtw88 phy_cond table walker.

Covers:
- bit decoding of marker words
- IF/ELSE/ENDIF branch selection
- ELIF chains
- nested no-match (skip all) vs match-one paths
- the four real 8821a tables (mac/agc/bb/rf_a) walk to a sensible cfg count
"""
from __future__ import annotations

import pytest

from wifit3.chips.rtl8821au.assets import (
    agc_tbl,
    bb_tbl,
    mac_tbl,
    rf_a_tbl,
)
from wifit3.chips.rtl8821au.phy_cond import (
    BRANCH_ELSE,
    BRANCH_ENDIF,
    BRANCH_IF,
    INTF_USB,
    DeviceCond,
    PhyCond,
    PhyCond2,
    parse_tbl_phy_cond,
)


def _awus036acs_cond() -> DeviceCond:
    """Best-known cond for the AWUS036ACS until EFUSE is read in M4b.

    The pcap will tell us the truth; for now we use the defaults that
    `rtw_phy_setup_phy_cond` falls back to: cut=15, pkg=15, intf=USB,
    rfe=0 (no external LNAs/PAs/BT advertised).
    """
    return DeviceCond(cut=15, pkg=15, intf=INTF_USB, rfe=0, cond2=PhyCond2())


# ---------------------------------------------------------------------------
# bit decoding
# ---------------------------------------------------------------------------

def test_decode_if_marker():
    c = PhyCond.decode(0x80000111)
    assert c.pos == 1
    assert c.neg == 0
    assert c.branch == BRANCH_IF
    assert c.cut == 0
    assert c.intf == 1            # USB
    assert c.rfe == 0x11          # bit0|bit4 (ext_lna_2g | btcoex)


def test_decode_else_marker():
    c = PhyCond.decode(0xA0000000)
    assert c.pos == 1 and c.branch == BRANCH_ELSE


def test_decode_endif_marker():
    c = PhyCond.decode(0xB0000000)
    assert c.pos == 1 and c.branch == BRANCH_ENDIF


def test_decode_neg_trigger():
    c = PhyCond.decode(0x40000000)
    assert c.pos == 0 and c.neg == 1


# ---------------------------------------------------------------------------
# walker — synthesised tiny tables
# ---------------------------------------------------------------------------

def _run(table, dev=None):
    dev = dev or _awus036acs_cond()
    seen: list[tuple[int, int]] = []
    n = parse_tbl_phy_cond(table, dev, lambda a, d: seen.append((a, d)))
    return n, seen


def test_no_markers_runs_everything():
    n, seen = _run([0x100, 0xAAAA, 0x200, 0xBBBB])
    assert n == 2
    assert seen == [(0x100, 0xAAAA), (0x200, 0xBBBB)]


def test_if_match_runs_block():
    # IF(intf=USB), cfg, ENDIF, cfg
    tbl = [
        0x80000100, 0x00000000,   # IF intf=USB
        0x40000000, 0x00000000,   # neg trigger
        0x100,      0xAAAA,
        0xB0000000, 0x00000000,   # ENDIF
        0x200,      0xBBBB,       # always-on tail
    ]
    n, seen = _run(tbl)
    assert n == 2
    assert seen == [(0x100, 0xAAAA), (0x200, 0xBBBB)]


def test_if_no_match_skips_block():
    # IF(intf=PCIE) but we are USB → skip
    tbl = [
        0x80000200, 0x00000000,   # IF intf=PCIE
        0x40000000, 0x00000000,
        0x100, 0xAAAA,
        0xB0000000, 0x00000000,
        0x200, 0xBBBB,
    ]
    n, seen = _run(tbl)
    assert n == 1
    assert seen == [(0x200, 0xBBBB)]


def test_if_else_picks_correct_branch():
    # IF(intf=PCIE)..ELSE..ENDIF — USB device should fall into ELSE.
    tbl = [
        0x80000200, 0x00000000,
        0x40000000, 0x00000000,
        0xAAA, 0x1,
        0xA0000000, 0x00000000,   # ELSE
        0xBBB, 0x2,
        0xB0000000, 0x00000000,
    ]
    n, seen = _run(tbl)
    assert seen == [(0xBBB, 0x2)]


def test_elif_chain_first_match_wins():
    # IF(intf=PCIE)..ELIF(intf=USB)..ELIF(intf=SDIO)..ELSE..ENDIF
    # USB device → take 2nd block only.
    tbl = [
        0x80000200, 0x00000000,
        0x40000000, 0x00000000,
        0x101, 0x1,
        0x90000100, 0x00000000,   # ELIF intf=USB (branch=1)
        0x40000000, 0x00000000,
        0x102, 0x2,
        0x90000400, 0x00000000,   # ELIF intf=SDIO
        0x40000000, 0x00000000,
        0x103, 0x3,
        0xA0000000, 0x00000000,
        0x104, 0x4,
        0xB0000000, 0x00000000,
    ]
    n, seen = _run(tbl)
    assert seen == [(0x102, 0x2)]


def test_no_branch_matches_falls_through_else():
    # IF(intf=PCIE)..ELIF(intf=SDIO)..ELSE..ENDIF — USB takes ELSE.
    tbl = [
        0x80000200, 0x00000000,
        0x40000000, 0x00000000,
        0x101, 0x1,
        0x90000400, 0x00000000,
        0x40000000, 0x00000000,
        0x102, 0x2,
        0xA0000000, 0x00000000,
        0x103, 0x3,
        0xB0000000, 0x00000000,
    ]
    n, seen = _run(tbl)
    assert seen == [(0x103, 0x3)]


# ---------------------------------------------------------------------------
# real tables — invariants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tbl, name",
    [
        (mac_tbl.TABLE, "mac"),
        (agc_tbl.TABLE, "agc"),
        (bb_tbl.TABLE, "bb"),
        (rf_a_tbl.TABLE, "rf_a"),
    ],
)
def test_real_table_walks_cleanly(tbl, name):
    """Walk with AWUS036ACS-ish defaults; smoke-check dispatched count."""
    n, _ = _run(tbl)
    # We can't pin exact numbers without EFUSE values from the pcap, but
    # `n` must be positive and less than total cfg-shaped tiles in the table.
    assert n > 0, f"{name} dispatched zero ops"
    assert n <= len(tbl) // 2, f"{name} dispatched > total tiles"


def test_mac_table_has_no_markers_so_n_equals_pairs():
    n, _ = _run(mac_tbl.TABLE)
    assert n == len(mac_tbl.TABLE) // 2
    assert n == 98


def test_bb_table_has_no_markers_so_n_equals_pairs():
    n, _ = _run(bb_tbl.TABLE)
    assert n == len(bb_tbl.TABLE) // 2
    assert n == 172
