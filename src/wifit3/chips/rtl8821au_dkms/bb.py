"""RTL8821AU (DKMS) M3 part 1: PHY_BBConfig8812 — BB enable + PHY_REG/AGC + xtal.

Ported from `PHY_BBConfig8812` (`rtl8812a_phycfg.c:370-411`), 8821a path. Order:
enable BB/RF in REG_SYS_FUNC_EN, PathA+PathB RF power-on, load PHY_REG then AGC_TAB
(both full-dword writes), then the crystal-cap read-modify-write at 0x2C[23:12].

# TODO(8812au): 8812 adds PHY_BB8812_Config_1T / rTxPath_Jaguar after RF config.
"""
from __future__ import annotations

from . import constants as C
from .bb_agc_tbl import BB_AGC_TAB
from .bb_phy_reg_tbl import BB_PHY_REG
from .phy_cond import JaguarParams, apply_table

# [SRC] hal_com_reg.h:1169-1171 (FEN_*), reg addrs below
FEN_BBRSTB = 0x01           # BIT0
FEN_BB_GLB_RSTn = 0x02      # BIT1
FEN_USBA = 0x04             # BIT2
REG_RF_CTRL = 0x001F        # PathA RF power
REG_OPT_CTRL_8812 = 0x0074  # +2 (0x0076) = PathB RF power
REG_RFE_CTRL_XTAL = 0x002C  # crystal-cap RMW target


def _bb_write(t, addr, val):
    # The 8821a BB PHY_REG / AGC tables contain no delay (0xfe..0xf9) pseudo-addrs,
    # so every taken row is a full MASKDWORD write. [SRC] phydm_regconfig8821a.c:130-158
    t.write32(addr, val)


def phy_bb_config(t, crystal_cap: int, params: JaguarParams | None = None) -> None:
    params = params or JaguarParams()
    # Enable BB + RF [SRC] rtl8812a_phycfg.c:382-395
    tmp = t.read8(C.REG_SYS_FUNC_EN) | FEN_USBA
    t.write8(C.REG_SYS_FUNC_EN, tmp)
    t.write8(C.REG_SYS_FUNC_EN, tmp | FEN_BB_GLB_RSTn | FEN_BBRSTB)
    t.write8(REG_RF_CTRL, 0x07)                     # PathA RF power on
    t.write8(REG_OPT_CTRL_8812 + 2, 0x07)           # PathB RF power on (unconditional, even 1x1)

    apply_table(BB_PHY_REG, lambda a, v: _bb_write(t, a, v), params)
    apply_table(BB_AGC_TAB, lambda a, v: _bb_write(t, a, v), params)

    # crystal_cap: 0x2C[23:12] = reg_val, reg_val = xcap | (xcap<<6) (ODM_RTL8821)
    # [SRC] phydm_set_crystal_cap_reg:218-243
    reg_val = (crystal_cap & 0x3F) | ((crystal_cap & 0x3F) << 6)
    cur = t.read32(REG_RFE_CTRL_XTAL)
    t.write32(REG_RFE_CTRL_XTAL, (cur & ~0x00FFF000) | ((reg_val << 12) & 0x00FFF000))
