"""RTL8822BU BB (baseband) init — PHYDM phy_reg + AGC tables.

rtl8822b_phy_init `[SRC] rtl8822b_phy.c:278` brackets the BB/RF tables with two PHYDM parameter
passes: PRE clears REG 0x808 bits 28/29 (disable OFDM/CCK) before the tables, POST sets them
(enable OFDM/CCK) after. Between them init_bb_reg applies the phy-reg table then the AGC table via
odm_config_bb_phy_8822b / odm_config_bb_agc_8822b (a 32-bit write per row; the 0xFA-0xFE delay
rows are absent from these tables on this card), then sets the crystal cap. The AGC table carries
cut/rfe conditionals, so it runs through phy_cond.walk rather than a flat loop. The 2T2R RX/TX
path (0x808 byte0 = 0x33) is baked into the phy-reg table value, not a separate write.
"""
from __future__ import annotations

from . import phy_cond
from .bb_agc_tbl import AGC_TAB
from .bb_phy_reg_tbl import BB_PHY_REG_TBL
from .constants import (
    REG_AFE_CTRL1, REG_AFE_CTRL2, XTAL_CAP_MASK_24, XTAL_CAP_MASK_28,
)

REG_OFDM0_TRX_PATH = 0x0808           # 0x808; bits 28/29 = OFDM/CCK block enable
OFDM_CCK_EN = (1 << 28) | (1 << 29)


def phy_parameter_init(t, post: bool) -> None:
    """[SRC] config_phydm_parameter_init_8822b: RMW 0x808 OFDM/CCK enable (PRE=off, POST=on)."""
    v = t.read32(REG_OFDM0_TRX_PATH)
    t.write32(REG_OFDM0_TRX_PATH, (v | OFDM_CCK_EN) if post else (v & ~OFDM_CCK_EN))


def phy_bb_config(t) -> None:
    """Apply the BB phy-reg table (flat 32-bit writes; no cut/rfe conditionals on this card)."""
    for addr, val in BB_PHY_REG_TBL:
        t.write32(addr, val)


def phy_agc_config(t, cfg: phy_cond.PhyCondConfig) -> None:
    """Apply the BB AGC table; phy_cond.walk selects the rows for this cut/rfe (W32 each).

    odm_config_bb_agc_8822b also feeds each 0x81C row to odm_update_agc_big_jump_lmt (software
    DIG state, no register write), so the wire is a plain run of W32s.
    """
    phy_cond.walk(AGC_TAB, cfg, lambda addr, val: t.write32(addr, val))


def set_crystal_cap(t, crystal_cap: int) -> None:
    """[SRC] phydm_set_crystal_cap_reg 8822b: 0x24[30:25] = 0x28[6:1] = crystal_cap (& 0x3F).

    The tail of init_bb_reg (rtw_phydm_set_crystal_cap). odm_set_mac_reg is a masked RMW, so
    each register is read-modify-written with the cap field shifted into its mask.
    """
    cap = crystal_cap & 0x3F
    v = t.read32(REG_AFE_CTRL1)
    t.write32(REG_AFE_CTRL1, (v & ~XTAL_CAP_MASK_24) | ((cap << 25) & XTAL_CAP_MASK_24))
    v = t.read32(REG_AFE_CTRL2)
    t.write32(REG_AFE_CTRL2, (v & ~XTAL_CAP_MASK_28) | ((cap << 1) & XTAL_CAP_MASK_28))
