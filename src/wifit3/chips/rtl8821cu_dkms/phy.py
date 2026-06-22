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

# rfe_type_expand values that set package_type=1 (the 0x2x combo range) [SRC] :345-357
_PKG_TYPE1_RFE = frozenset(
    (0x22, 0x24, 0x27, 0x2A, 0x2C, 0x2F, 0x20, 0x21, 0x23, 0x25, 0x26, 0x28, 0x29, 0x2B, 0x2D, 0x2E))


def init_hw_info_by_rfe(t, rfe_type: int) -> None:
    """[SRC] phydm_init_hw_info_by_rfe_type_8821c phydm_hal_api8821c.c:366-372 — the DPDT default
    for `0xCB4` keyed on the RFE module type (package-1 BTG range / RFE 4 / everything else)."""
    pkg1 = rfe_type in _PKG_TYPE1_RFE
    if pkg1 and 0x28 <= rfe_type <= 0x2F:
        val = 0x00000073
    elif rfe_type == 4:
        val = 0x20000077
    else:
        val = 0x10000077
    t.write32(REG_DPDT_CTRL, val)
