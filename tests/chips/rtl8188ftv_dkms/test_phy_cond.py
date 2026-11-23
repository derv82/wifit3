"""rtl8188ftv_dkms: phydm conditional-table walker semantics."""


def _walk(table, cut_version, package_type):
    from wifit3.chips.rtl8188ftv_dkms import phy_cond
    out = []
    phy_cond.walk_table(table, lambda a, v: out.append((a, v)), cut_version, package_type)
    return out


def test_straight_rows_emit():
    assert _walk([0x100, 0xAA, 0x104, 0xBB], 1, 7) == [(0x100, 0xAA), (0x104, 0xBB)]


def test_if_else_endif_matching_branch():
    b31, b30 = 1 << 31, 1 << 30
    table = [
        b31 | (0 << 28), 0x00040200,   # IF cond1: platform CE + interface USB
        b30, 0x00000000,
        0x100, 0xAA,
        b31 | (2 << 28), 0,            # ELSE
        0x104, 0xBB,
        b31 | (3 << 28), 0,            # ENDIF
        0x108, 0xCC,
    ]
    assert _walk(table, 1, 7) == [(0x100, 0xAA), (0x108, 0xCC)]


def test_if_else_endif_other_branch():
    b31, b30 = 1 << 31, 1 << 30
    table = [
        b31 | (0 << 28) | 0x01, 0x000000FF,   # IF: GLNA lane, needs type 0xFF
        b30, 0x00000000,
        0x100, 0xAA,
        b31 | (2 << 28), 0,            # ELSE
        0x104, 0xBB,
        b31 | (3 << 28), 0,            # ENDIF
    ]
    assert _walk(table, 1, 7) == [(0x104, 0xBB)]


def test_driver1_matches_check_positive_layout():
    from wifit3.chips.rtl8188ftv_dkms import phy_cond
    # halhwimg8188f_mac.c:41-46: cut [27:24], platform [23:16], package [15:12], interface [11:8]
    assert phy_cond.driver1(1, 7) == 0x01047200
    assert phy_cond.driver1(2, 0) == 0x02040200


def test_check_positive_compares_package_type():
    from wifit3.chips.rtl8188ftv_dkms import phy_cond
    assert phy_cond.check_positive(0x00007000, 0, 0, 1, 7) is True
    assert phy_cond.check_positive(0x00007000, 0, 0, 1, 1) is False
    assert phy_cond.check_positive(0x00001000, 0, 0, 1, 1) is True


def test_rf_table_cut_selects_rf51_row():
    from wifit3.chips.rtl8188ftv_dkms import rf_radio_a_tbl
    # halhwimg8188f_rf.c Array_MP_8188F_RadioA: `if cut 1` RF 0x51 = 0xE8231, else 0xE8333
    b_cut = _walk(rf_radio_a_tbl.TABLE, 1, 7)
    c_cut = _walk(rf_radio_a_tbl.TABLE, 2, 7)
    assert (0x51, 0x000E8231) in b_cut and (0x51, 0x000E8333) not in b_cut
    assert (0x51, 0x000E8333) in c_cut and (0x51, 0x000E8231) not in c_cut
    assert b_cut != c_cut


def test_tables_have_no_package_condition():
    from wifit3.chips.rtl8188ftv_dkms import (bb_agc_tab_tbl, bb_phy_reg_tbl, mac_reg_tbl,
                                              rf_radio_a_tbl)
    for tbl in (mac_reg_tbl, bb_phy_reg_tbl, bb_agc_tab_tbl, rf_radio_a_tbl):
        for package_type in range(8):
            assert _walk(tbl.TABLE, 1, package_type) == _walk(tbl.TABLE, 1, 7)


def test_package_type_from_hidden_report():
    from wifit3.chips.rtl8188ftv_dkms import c2h
    report = bytes([0, 0, 0, 0, 0x72, 0, 0, 0])   # byte 4: package 7, HCI USB
    assert c2h.get_c2h_mac_hidden_rpt_package_type(report) == 7
    assert c2h.get_c2h_mac_hidden_rpt_package_type(b"") == 0
