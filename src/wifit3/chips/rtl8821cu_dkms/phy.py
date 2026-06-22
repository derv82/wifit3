"""RTL8821CU PHYDM (BB/RF dynamic-mechanism) bring-up — the slice reached during cold init.

So far this is just the one-time RFE-type init phydm runs while bringing the BB up: it decodes
the EFUSE RFE module type into the default 2.4G RF set (WLG/BTG) and antenna, and writes the
DPDT default to `0xCB4`. Only that register write reaches the wire; the rest sets phydm state.
The full BB/RF init (PHY parameter tables, RF calibration, channel tune) lands here in later
milestones.

Ported from [SRC] hal/phydm/rtl8821c/phydm_hal_api8821c.c:328
phydm_init_hw_info_by_rfe_type_8821c (via phydm_init_hw_info_by_rfe phydm.c:222).
"""
from __future__ import annotations

REG_DPDT_CTRL = 0x0CB4              # BB DPDT/SPDT antenna-switch control (4-byte default)

SWITCH_TO_BTG, SWITCH_TO_WLG = 0, 1  # [SRC] phydm_hal_api8821c.h:33 enum

# rfe_type_expand -> default 2.4G RF set [SRC] phydm_hal_api8821c.c:339-356
_RF_SET_BTG = frozenset((2, 4, 7, 0x22, 0x24, 0x27, 0x2A, 0x2C, 0x2F))
_RF_SET_WLG = frozenset(
    (0, 1, 3, 5, 6, 0x20, 0x21, 0x23, 0x25, 0x26, 0x28, 0x29, 0x2B, 0x2D, 0x2E))
# rfe_type_expand values that set package_type=1 (the 0x2x combo range) [SRC] :345-357
_PKG_TYPE1_RFE = frozenset(
    (0x22, 0x24, 0x27, 0x2A, 0x2C, 0x2F, 0x20, 0x21, 0x23, 0x25, 0x26, 0x28, 0x29, 0x2B, 0x2D, 0x2E))


def init_hw_info_by_rfe(t, info) -> None:
    """[SRC] phydm_init_hw_info_by_rfe_type_8821c phydm_hal_api8821c.c:328 — the DPDT default for
    `0xCB4` keyed on the RFE module type, plus the two PHYDM-table discriminators this sets:
    `dm->rfe_type = rfe_type_expand >> 3` (:336) and the `package_type = 1` override for the 0x2x
    combo range (:349/356). The 0xCB4 write is the only one that reaches the wire here; the
    transformed rfe/package feed the later BB/RF parameter-table walker (they differ from the
    hal->rfe_type / hal->PackageType the general-info H2C used)."""
    rfe_type = info.rfe_type        # rfe_type_expand (raw EFUSE board option)
    pkg1 = rfe_type in _PKG_TYPE1_RFE
    info.phydm_rfe_type = rfe_type >> 3
    info.phydm_package_type = 1 if pkg1 else info.package_type
    if rfe_type in _RF_SET_BTG:
        info.default_rf_set = SWITCH_TO_BTG
    elif rfe_type in _RF_SET_WLG:
        info.default_rf_set = SWITCH_TO_WLG
    if info.phydm_package_type == 1 and 0x28 <= rfe_type <= 0x2F:
        val = 0x00000073
    elif rfe_type == 4:
        val = 0x20000077
    else:
        val = 0x10000077
    t.write32(REG_DPDT_CTRL, val)
