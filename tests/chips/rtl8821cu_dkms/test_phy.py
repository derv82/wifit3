"""Hardware-free regression for the rfe-derived PHYDM discriminators.

`phydm_init_hw_info_by_rfe_type_8821c` resolves the raw EFUSE board option into the phydm table
discriminators (rfe>>3, package override), the default 2.4 GHz RF set (BTG/WLG), and the DPDT
default (0xcb4). The pcap card is rfe_type_expand 0x22 (BTG, package-1); these pin the branch for
non-reference burns. [SRC] hal/phydm/rtl8821c/phydm_hal_api8821c.c:328.
"""
from types import SimpleNamespace

from wifit3.chips.rtl8821cu_dkms import phy

_DPDT = phy.REG_DPDT_CTRL     # 0x0CB4


class Rec:
    def __init__(self):
        self.ops = []

    def write32(self, a, v):
        self.ops.append(("W32", a, v))


def _info(rfe, package=0):
    return SimpleNamespace(rfe_type=rfe, package_type=package, phydm_rfe_type=0,
                           phydm_package_type=0, default_rf_set=1)


def test_reference_rfe0x22_btg_package1():
    rec, info = Rec(), _info(0x22)
    phy.init_hw_info_by_rfe(rec, info)
    assert info.phydm_rfe_type == 0x22 >> 3        # 4
    assert info.default_rf_set == phy.SWITCH_TO_BTG
    assert info.phydm_package_type == 1            # 0x22 in the package-1 combo range
    assert ("W32", _DPDT, 0x10000077) in rec.ops   # not the 0x28-0x2f range, not rfe==4


def test_variant_wlg_rfe0_default_dpdt():
    rec, info = Rec(), _info(0)
    phy.init_hw_info_by_rfe(rec, info)
    assert info.phydm_rfe_type == 0
    assert info.default_rf_set == phy.SWITCH_TO_WLG
    assert info.phydm_package_type == 0            # not a package-1 rfe -> hal package (0 here)
    assert ("W32", _DPDT, 0x10000077) in rec.ops


def test_variant_rfe4_btg_uses_0x20000077_dpdt():
    rec, info = Rec(), _info(4)
    phy.init_hw_info_by_rfe(rec, info)
    assert info.default_rf_set == phy.SWITCH_TO_BTG
    assert info.phydm_package_type == 0
    assert ("W32", _DPDT, 0x20000077) in rec.ops   # rfe==4 arm


def test_variant_package1_high_range_uses_0x73_dpdt():
    rec, info = Rec(), _info(0x28)
    phy.init_hw_info_by_rfe(rec, info)
    assert info.phydm_rfe_type == 0x28 >> 3        # 5
    assert info.default_rf_set == phy.SWITCH_TO_WLG
    assert info.phydm_package_type == 1
    assert ("W32", _DPDT, 0x00000073) in rec.ops   # package-1 && 0x28<=rfe<=0x2f
