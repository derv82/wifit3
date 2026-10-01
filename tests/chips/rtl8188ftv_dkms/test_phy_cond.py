"""rtl8188ftv_dkms M5a: phydm conditional-table walker semantics."""


def test_straight_rows_emit():
    from wifit3.chips.rtl8188ftv_dkms import phy_cond
    out = []
    phy_cond.walk_table([0x100, 0xAA, 0x104, 0xBB], lambda a, v: out.append((a, v)))
    assert out == [(0x100, 0xAA), (0x104, 0xBB)]


def test_if_else_endif_matching_branch():
    from wifit3.chips.rtl8188ftv_dkms import phy_cond
    b31, b30 = 1 << 31, 1 << 30
    table = [
        b31 | (0 << 28), 0x00040200,   # IF cond1
        b30, 0x00000000,               # negative word: matches DRIVER1
        0x100, 0xAA,                   # taken
        b31 | (2 << 28), 0,            # ELSE
        0x104, 0xBB,                   # skipped
        b31 | (3 << 28), 0,            # ENDIF
        0x108, 0xCC,                   # taken
    ]
    out = []
    phy_cond.walk_table(table, lambda a, v: out.append((a, v)))
    assert out == [(0x100, 0xAA), (0x108, 0xCC)]


def test_if_else_endif_other_branch():
    from wifit3.chips.rtl8188ftv_dkms import phy_cond
    b31, b30 = 1 << 31, 1 << 30
    table = [
        b31 | (0 << 28) | 0x01, 0x000000FF,   # IF: GLNA lane, needs type 0xFF
        b30, 0x00000000,               # negative word: driver type 0x00 mismatches
        0x100, 0xAA,                   # skipped
        b31 | (2 << 28), 0,            # ELSE
        0x104, 0xBB,                   # taken
        b31 | (3 << 28), 0,            # ENDIF
    ]
    out = []
    phy_cond.walk_table(table, lambda a, v: out.append((a, v)))
    assert out == [(0x104, 0xBB)]


def test_check_positive_value_checks():
    from wifit3.chips.rtl8188ftv_dkms import phy_cond
    assert phy_cond.check_positive(0x00000000, 0, 0) is True
    assert phy_cond.check_positive(0x00001000, 0, 0) is False   # package QFN, driver 7
    assert phy_cond.check_positive(0x00007000, 0, 0) is True    # package 7 matches
    assert phy_cond.check_positive(0x01000000, 0, 0) is True    # cut 1 matches
    assert phy_cond.check_positive(0x02000000, 0, 0) is False   # cut 2 mismatches
