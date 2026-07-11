"""Hardware-free regression for the RTL8188EUS (DKMS) phydm conditional-table walker.

Locks that (a) the reference card's internal-PA/LNA driver words reproduce the module
defaults so the pcap walk is byte-identical, and (b) an external-LNA/PA burn actually
reaches board-gated branches in the init tables (the generalization is live, not dead).
"""
from wifit3.chips.rtl8188eus_dkms import phy_cond
from wifit3.chips.rtl8188eus_dkms.bb_agc_tab_tbl import AGC_TAB
from wifit3.chips.rtl8188eus_dkms.bb_phy_reg_tbl import PHY_REG
from wifit3.chips.rtl8188eus_dkms.mac_reg_tbl import MAC_REG
from wifit3.chips.rtl8188eus_dkms.rf_radio_a_tbl import RADIO_A


def _walk(table, dw=None):
    out = []
    if dw is None:
        phy_cond.walk_table(table, lambda a, d: out.append((a, d)))
    else:
        phy_cond.walk_table(table, lambda a, d: out.append((a, d)), *dw)
    return out


def test_internal_driver_words_match_module_defaults():
    # Reference card (internal PA+LNA, TypeGLNA 0) -> exactly the module DRIVER* words.
    dw = phy_cond.build_driver_words(False, False, 0x0)
    assert dw == (phy_cond.DRIVER1, phy_cond.DRIVER2, phy_cond.DRIVER4)
    # ...and walking with them equals the default walk (byte-identical) on every table.
    for table in (MAC_REG, PHY_REG, AGC_TAB, RADIO_A):
        assert _walk(table, dw) == _walk(table)


def test_external_board_bits_and_type_glna_packing():
    # ext-LNA sets _board_type bit0 (GLNA); ext-PA sets bit1 (GPA); type_glna -> driver2.
    d1, d2, d4 = phy_cond.build_driver_words(True, False, 0x2)
    assert d1 & 0x1F == 0b01          # GLNA only
    assert (d2, d4) == (0x2, 0x0)
    d1, _, _ = phy_cond.build_driver_words(False, True, 0x0)
    assert d1 & 0x1F == 0b10          # GPA only
    d1, _, _ = phy_cond.build_driver_words(True, True, 0x1)
    assert d1 & 0x1F == 0b11          # GLNA + GPA
    # the non-board bits of driver1 are preserved from the reference word.
    assert d1 & ~0x1F == phy_cond.DRIVER1 & ~0x1F


def test_external_lna_changes_agc_walk():
    # An ext-LNA burn must reach a board-gated AGC branch (else the port is still
    # hardcoded to the internal card). RADIO_A likewise reacts to ext-PA.
    base = _walk(AGC_TAB)
    ext_lna = _walk(AGC_TAB, phy_cond.build_driver_words(True, False, 0x1))
    assert ext_lna != base
    rf_base = _walk(RADIO_A)
    rf_ext_pa = _walk(RADIO_A, phy_cond.build_driver_words(False, True, 0x0))
    assert rf_ext_pa != rf_base
