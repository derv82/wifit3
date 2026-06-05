"""RTL8812AU M3 part 1: PHY_BBConfig8812 — BB enable + PHY_REG/AGC + crystal cap.

Ported from ``PHY_BBConfig8812`` / ``phy_BB8812_Config_ParaFile`` (rtl8812a_phycfg.c:
307-413), 8812AU path. Order: enable BB/RF in REG_SYS_FUNC_EN (FEN_USBA for USB),
PathA + PathB RF power-on, load the 8812a PHY_REG then AGC_TAB (full-dword writes via
the JaguarSeries walker), then the crystal-cap RMW.

Structurally identical to the 8821 BB config; the deltas are the 8812a tables and the
crystal-cap **bit field**: the 8812 writes 0x2C[30:25]=0x2C[24:19] (mask 0x7FF80000),
where the 8821 used 0x2C[23:12] (mask 0x00FFF000) — same reg_val, different position
[SRC phydm_set_crystal_cap_reg, ODM_RTL8812 branch]. The PHY_REG_PG (by-rate) and the
AGC diff_lb/hb tables are not loaded here (TXPWR_BY_RATE_EN=0; the band diff tables are
a 5 GHz concern).
"""
from __future__ import annotations

from ..rtl88xxau_base import registers as R
from ..rtl88xxau_base.phy_cond import JaguarParams, apply_table
from .bb_agc_tbl import BB_AGC_TAB
from .bb_phy_reg_tbl import BB_PHY_REG

# [SRC] hal_com_reg.h:1169-1171 (FEN_*), reg addrs below
FEN_BBRSTB = 0x01           # BIT0
FEN_BB_GLB_RSTn = 0x02      # BIT1
FEN_USBA = 0x04             # BIT2
REG_RF_CTRL = 0x001F        # PathA RF power
REG_OPT_CTRL_8812 = 0x0074  # +2 (0x0076) = PathB RF power
REG_RFE_CTRL_XTAL = 0x002C  # crystal-cap RMW target
_XTAL_MASK_8812 = 0x7FF80000  # 0x2C[30:19] (ODM_RTL8812; 8821 used 0x00FFF000)


def _bb_write(t, addr, val):
    # The 8812a BB PHY_REG / AGC tables resolve (via phy_cond) to full MASKDWORD writes;
    # no delay pseudo-addrs in the BB tables. [SRC] odm_config_bb_8812a
    t.write32(addr, val)


def phy_bb_config(t, crystal_cap: int, params: JaguarParams | None = None) -> None:
    params = params or JaguarParams()
    # Enable BB + RF [SRC] PHY_BBConfig8812 (8812AU -> FEN_USBA)
    tmp = t.read8(R.REG_SYS_FUNC_EN) | FEN_USBA
    t.write8(R.REG_SYS_FUNC_EN, tmp)
    t.write8(R.REG_SYS_FUNC_EN, tmp | FEN_BB_GLB_RSTn | FEN_BBRSTB)
    t.write8(REG_RF_CTRL, 0x07)                     # PathA RF power on
    t.write8(REG_OPT_CTRL_8812 + 2, 0x07)           # PathB RF power on

    apply_table(BB_PHY_REG, lambda a, v: _bb_write(t, a, v), params)
    apply_table(BB_AGC_TAB, lambda a, v: _bb_write(t, a, v), params)

    # crystal_cap: 0x2C[30:19] = reg_val, reg_val = xcap | (xcap<<6) (ODM_RTL8812)
    # [SRC] phydm_set_crystal_cap_reg:225-232
    reg_val = (crystal_cap & 0x3F) | ((crystal_cap & 0x3F) << 6)
    cur = t.read32(REG_RFE_CTRL_XTAL)
    t.write32(REG_RFE_CTRL_XTAL, (cur & ~_XTAL_MASK_8812) | ((reg_val << 19) & _XTAL_MASK_8812))
