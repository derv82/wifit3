"""RTL8922AU BB register init, ported from rtw89-7.2 phy.c / phy_be.c.

rtw89_phy_init_bb_reg applies the firmware's BB register table for PHY_0 and (DBCC) PHY_1. The
table is condition-coded: a headline block picks the (rfe_type, cv) branch, then if/elif/else
markers select which register writes apply under it. [SRC] phy.c:1940-1966.
"""
import time

from .constants import (
    RTW89_FW_ELEMENT_ID_BB_REG, CR_BASE_BE, BYPASS_CR_DATA,
    PHY_HEADLINE_VALID, PHY_COND_BRANCH_IF, PHY_COND_BRANCH_ELIF, PHY_COND_BRANCH_ELSE,
    PHY_COND_BRANCH_END, PHY_COND_CHECK, PHY_COND_DONT_CARE,
    R_BE_FEN_RST_ENABLE, B_BE_FEN_BBPLAT_RSTB, B_BE_FEN_BB1PLAT_RSTB,
    B_BE_BOOT_RDY0, B_BE_BOOT_RDY1, R_BBCLK, B_CLK_640M, R_TXSCALE, B_TXFCTR_EN,
    R_TXFCTR, B_TXFCTR_THD, R_SLOPE, B_EHT_RATE_TH, B_SLOPE_A, B_SLOPE_B,
    R_BEDGE, B_HE_RATE_TH, B_EHT_MCS14, R_BEDGE2, B_HT_VHT_TH, B_EHT_MCS15,
    R_BEDGE3, B_EHTTB_EN, B_HEERSU_EN, B_HEMU_EN, B_TB_EN, R_SU_PUNC, B_SU_PUNC_EN,
    R_BEDGE5, B_HWGEN_EN, B_PWROFST_COMP, R_MAG_AB, B_BY_SLOPE, B_MAG_AB,
    R_MAG_A, B_MGA_AEND, R_SC_CORNER, B_SC_CORNER, R_UDP_COEEF, B_UDP_COEEF,
)
from . import firmware


def _phy0_phy1_offset(addr: int) -> int:
    """rtw89_phy0_phy1_offset_be: the PHY_0->PHY_1 register address delta. [SRC] phy_be.c:228-244."""
    pg = addr >> 8
    if ((0x4 <= pg <= 0xF) or (0x20 <= pg <= 0x2B) or (0x40 <= pg <= 0x4F)
            or (0x60 <= pg <= 0x6F) or (0xE4 <= pg <= 0xE5) or (0xE8 <= pg <= 0xED)):
        return 0x1000
    return 0


_DELAYS = {0xFE: 0.050, 0xFD: 0.005, 0xFC: 0.001, 0xFB: 50e-6, 0xFA: 5e-6, 0xF9: 1e-6}


def _config_bb_reg(t, addr: int, data: int, phy1: bool) -> None:
    """rtw89_phy_config_bb_reg: the flow-control delay opcodes and CR bypass, else a BB register
    write at addr + cr_base (with the PHY_1 offset). [SRC] phy.c:1402-1431."""
    if addr in _DELAYS:
        time.sleep(_DELAYS[addr])
    elif data == BYPASS_CR_DATA:
        return
    else:
        if phy1:
            addr += _phy0_phy1_offset(addr)
        t.write32(addr + CR_BASE_BE, data)


def _sel_headline(regs: list, rfe: int, cv: int) -> tuple:
    """rtw89_phy_sel_headline: choose the headline index whose (rfe, cv) target best matches.
    [SRC] phy.c:1785-1868."""
    hs = 0
    for a, _ in regs:
        if (a >> 28) != PHY_HEADLINE_VALID:
            break
        hs += 1
    if hs == 0:
        return 0, 0

    def target(a):
        return a & 0x0FFFFFFF

    def compare(r, c):
        return ((r & 0xFF) << 16) | (c & 0xFF)

    for want in (compare(rfe, cv), compare(rfe, PHY_COND_DONT_CARE)):   # case 1, case 2
        for i in range(hs):
            if target(regs[i][0]) == want:
                return hs, i
    for want_rfe in (rfe, PHY_COND_DONT_CARE):                          # case 3, case 4
        cv_max = 0
        idx = None
        for i in range(hs):
            if ((regs[i][0] >> 16) & 0xFF) == want_rfe and (regs[i][0] & 0xFF) >= cv_max:
                cv_max = regs[i][0] & 0xFF
                idx = i
        if idx is not None:
            return hs, idx
    return hs, 0


def _init_reg(t, regs: list, hs: int, hidx: int, phy1: bool) -> None:
    """rtw89_phy_init_reg's conditional walk: apply each register write under the branch selected
    by cfg_target. [SRC] phy.c:1896-1936."""
    cfg_target = regs[hidx][0] & 0x0FFFFFFF
    is_matched = True
    target_found = False
    target = 0
    for i in range(hs, len(regs)):
        a, d = regs[i]
        cond = a >> 28
        if cond in (PHY_COND_BRANCH_IF, PHY_COND_BRANCH_ELIF):
            target = a & 0x0FFFFFFF
        elif cond == PHY_COND_BRANCH_ELSE:
            is_matched = False
            if not target_found:
                return                       # malformed table: the kernel warns and bails
        elif cond == PHY_COND_BRANCH_END:
            is_matched = True
            target_found = False
        elif cond == PHY_COND_CHECK:
            if target_found:
                is_matched = False
            elif target == cfg_target:
                is_matched = True
                target_found = True
            else:
                is_matched = False
                target_found = False
        elif is_matched:
            _config_bb_reg(t, a, d, phy1)


def init_bb_reg(t, cv: int) -> None:
    """rtw89_phy_init_bb_reg for the 8922A: apply the firmware BB register table for PHY_0 then
    (DBCC) PHY_1. init_txpwr_unit and bb_reset are no-ops on this chip, and bb_gain populates
    software gain arrays only. [SRC] phy.c:1940-1966, rtw8922a.c:3101,1923."""
    regs = firmware.element_regs(RTW89_FW_ELEMENT_ID_BB_REG)
    hs, hidx = _sel_headline(regs, t.rfe_type, cv)
    _init_reg(t, regs, hs, hidx, phy1=False)
    _init_reg(t, regs, hs, hidx, phy1=True)          # dbcc_en is always true on the 8922A


def _phy_write32_mask(t, addr: int, mask: int, data: int) -> None:
    """rtw89_phy_write32_mask: masked BB register RMW at addr + cr_base. [SRC] phy.h:775."""
    t.write32_mask(addr + CR_BASE_BE, mask, data)


def _phy_write32_idx(t, addr: int, mask: int, data: int, phy_idx: int) -> None:
    """rtw89_phy_write32_idx: masked BB RMW, PHY_1 shifted by the phy0/phy1 offset. [SRC] phy.c:2170."""
    if phy_idx == 1:
        addr += _phy0_phy1_offset(addr)
    _phy_write32_mask(t, addr, mask, data)


def _set_phy_regs(t, addr: int, mask: int, val: int) -> None:
    """rtw89_phy_set_phy_regs: write the field on both PHY_0 and PHY_1 (DBCC). [SRC] phy.c:2206."""
    _phy_write32_idx(t, addr, mask, val, 0)
    _phy_write32_idx(t, addr, mask, val, 1)


_BBRST_MASK = (B_BE_FEN_BBPLAT_RSTB, B_BE_FEN_BB1PLAT_RSTB)     # rtw8922a.c:1798
_MCU_BOOTRDY_MASK = (B_BE_BOOT_RDY0, B_BE_BOOT_RDY1)           # rtw8922a.c:1800


def _bb_postinit(t, phy_idx: int) -> None:
    """rtw8922a_bb_postinit: FEN resets (MCU boot-ready on PHY_0, BB reset per phy), then the BB
    rate-edge / slope / magnitude register block written on both phys. [SRC] rtw8922a.c:1820-1849."""
    if phy_idx == 0:
        t.write32_set(R_BE_FEN_RST_ENABLE, _MCU_BOOTRDY_MASK[phy_idx])
    t.write32_set(R_BE_FEN_RST_ENABLE, _BBRST_MASK[phy_idx])

    t.write32_set(R_BBCLK + CR_BASE_BE, B_CLK_640M)
    t.write32_clr(R_TXSCALE + CR_BASE_BE, B_TXFCTR_EN)
    _set_phy_regs(t, R_TXFCTR, B_TXFCTR_THD, 0x200)
    _set_phy_regs(t, R_SLOPE, B_EHT_RATE_TH, 0xA)
    _set_phy_regs(t, R_BEDGE, B_HE_RATE_TH, 0xA)
    _set_phy_regs(t, R_BEDGE2, B_HT_VHT_TH, 0xAAA)
    _set_phy_regs(t, R_BEDGE, B_EHT_MCS14, 0x1)
    _set_phy_regs(t, R_BEDGE2, B_EHT_MCS15, 0x1)
    _set_phy_regs(t, R_BEDGE3, B_EHTTB_EN, 0x0)
    _set_phy_regs(t, R_BEDGE3, B_HEERSU_EN, 0x0)
    _set_phy_regs(t, R_BEDGE3, B_HEMU_EN, 0x0)
    _set_phy_regs(t, R_BEDGE3, B_TB_EN, 0x0)
    _set_phy_regs(t, R_SU_PUNC, B_SU_PUNC_EN, 0x1)
    _set_phy_regs(t, R_BEDGE5, B_HWGEN_EN, 0x1)
    _set_phy_regs(t, R_BEDGE5, B_PWROFST_COMP, 0x1)
    _set_phy_regs(t, R_MAG_AB, B_BY_SLOPE, 0x1)
    _set_phy_regs(t, R_MAG_A, B_MGA_AEND, 0xE0)
    _set_phy_regs(t, R_MAG_AB, B_MAG_AB, 0xE0C000)
    _set_phy_regs(t, R_SLOPE, B_SLOPE_A, 0x3FE0)
    _set_phy_regs(t, R_SLOPE, B_SLOPE_B, 0x3FE0)
    _set_phy_regs(t, R_SC_CORNER, B_SC_CORNER, 0x200)
    _phy_write32_idx(t, R_UDP_COEEF, B_UDP_COEEF, 0x0, phy_idx)
    _phy_write32_idx(t, R_UDP_COEEF, B_UDP_COEEF, 0x1, phy_idx)


def chip_bb_postinit(t) -> None:
    """rtw89_chip_bb_postinit: run bb_postinit for PHY_0 then (DBCC) PHY_1. [SRC] core.h/phy.c."""
    _bb_postinit(t, 0)
    _bb_postinit(t, 1)
