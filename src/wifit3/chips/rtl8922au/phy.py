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
