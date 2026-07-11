"""RTL8188EUS baseband (BB) + AGC configuration (M2b).

``PHY_BBConfig8188E`` [SRC] rtl8188e_phycfg.c:964: enable BB/RF clocks, load the
PHY_REG and AGC_TAB tables through the phydm walker (full-32-bit writes), then
apply the crystal-cap AFE trim.

PHY_REG data rows go through ``odm_config_bb_phy_8188e`` (addresses 0xF9..0xFE are
settling delays, no register write); AGC_TAB rows through ``odm_config_bb_agc_8188e``
(always a write). Both are ``odm_set_bb_reg(addr, MASKDWORD, data)`` = a plain
write32. [WIRE] cap1 ops 890.. (BB prologue) through the crystal-cap write at 0x24.
"""
from __future__ import annotations

from . import phy_cond
from .bb_agc_tab_tbl import AGC_TAB
from .bb_phy_reg_tbl import PHY_REG
from .constants import (
    BIT,
    bCCKEn,
    bOFDMEn,
    BB_DELAY_ADDRS,
    DEFAULT_CRYSTAL_CAP,
    FEN_BB_USB,
    REG_AFE_XTAL_CTRL,
    REG_RF_CTRL,
    REG_SYS_FUNC_EN,
    rFPGA0_RFMOD,
    RF_CTRL_INIT,
    SYS_FUNC_BB_ENABLE,
    XTAL_CAP_MASK,
)


def phy_bb_config(t, crystal_cap: int = DEFAULT_CRYSTAL_CAP, driver_words=None) -> None:
    # Enable BB and RF.
    reg = t.read16(REG_SYS_FUNC_EN)
    t.write16(REG_SYS_FUNC_EN, (reg | SYS_FUNC_BB_ENABLE) & 0xFFFF)
    t.write8(REG_RF_CTRL, RF_CTRL_INIT)
    t.write8(REG_SYS_FUNC_EN, FEN_BB_USB)

    # Config BB (PHY_REG) and AGC (AGC_TAB). driver_words (d1,d2,d4) gates the
    # board/LNA-conditional rows; None = the reference card's internal-PA/LNA walk.
    d1, d2, d4 = driver_words if driver_words else (None, None, None)
    phy_cond.walk_table(PHY_REG, _emit_phy(t), d1, d2, d4)
    phy_cond.walk_table(AGC_TAB, lambda addr, data: t.write32(addr, data), d1, d2, d4)

    set_crystal_cap(t, crystal_cap)


def _emit_phy(t):
    def emit(addr: int, data: int) -> None:
        if addr in BB_DELAY_ADDRS:        # settling delay, not a register write
            return
        t.write32(addr, data)
    return emit


# --- BB register helpers (phy_set_bb_reg / phy_query_bb_reg) ---------------
def set_bb_reg(t, addr: int, mask: int, data: int) -> None:
    """Masked BB write [SRC] phy_set_bb_reg — full-mask is a direct write32,
    else read-modify-write at the mask's lowest set bit."""
    if mask == 0xFFFFFFFF:
        t.write32(addr, data & 0xFFFFFFFF)
        return
    shift = (mask & -mask).bit_length() - 1
    cur = t.read32(addr)
    t.write32(addr, (cur & ~mask) | ((data << shift) & mask))


def query_bb_reg(t, addr: int, mask: int) -> int:
    """Masked BB read [SRC] phy_query_bb_reg."""
    shift = (mask & -mask).bit_length() - 1
    return (t.read32(addr) & mask) >> shift


def set_crystal_cap(t, crystal_cap: int) -> None:
    """``hal_set_crystal_cap`` (8188E): 0x24[22:11] = cap | (cap<<6). [SRC] hal_com.c."""
    cap = crystal_cap & 0x3F
    set_bb_reg(t, REG_AFE_XTAL_CTRL, XTAL_CAP_MASK, cap | (cap << 6))


def bb_turn_on_block(t) -> None:
    """``_BBTurnOnBlock`` [SRC] usb_halinit.c:1039 — enable the CCK and OFDM blocks
    in rFPGA0_RFMOD (0x800), each a separate masked RMW."""
    set_bb_reg(t, rFPGA0_RFMOD, bCCKEn, 0x1)
    set_bb_reg(t, rFPGA0_RFMOD, bOFDMEn, 0x1)


# RFE control BB registers [SRC] PHY_SetRFEReg_8188E rtl8188e_phycfg.c:2007-2009 (the
# vendor uses these addresses as literals; no reg.h symbol exists).
_REG_RFE_CTRL = 0x40      # 0x40[3:2] = 0x3
_REG_RFE_PINMUX = 0xEE8   # 0xEE8[28] = 0x1
_REG_RFE_INV = 0x87C      # 0x87C[0] = 0x0


def phy_set_rfe_reg(t, board) -> None:
    """``PHY_SetRFEReg_8188E`` [SRC] rtl8188e_phycfg.c:1993, called from the MISC11 tail
    [SRC] usb_halinit.c:1568. Early-returns (no wire ops) unless the board has an
    external PA or LNA, so this dev card (internal PA+LNA) is unaffected. The switch on
    rfe_type has only a case-0/default arm, so the three writes are rfe_type-independent
    (88EU rfe_type is always 0)."""
    if not (board.external_pa_2g or board.external_lna_2g):
        return
    set_bb_reg(t, _REG_RFE_CTRL, BIT(2) | BIT(3), 0x3)     # 0x3 << 2
    set_bb_reg(t, _REG_RFE_PINMUX, BIT(28), 0x1)
    set_bb_reg(t, _REG_RFE_INV, BIT(0), 0x0)
