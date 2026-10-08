"""RTL8188FTV DKMS BB/MAC register RMW helpers + BB config (M5a/b).

``query/set_bb_reg`` ported from ``PHY_QueryBBReg_8188F`` /
``PHY_SetBBReg_8188F`` (hal/rtl8188f/rtl8188f_phycfg.c:78-162) with
``phy_CalculateBitShift`` (phycfg.c:62-76). ``PHY_SetMacReg`` is the same
function (include/hal_intf.h:605). Full-dword writes skip the readback.
``bb_config`` ported from ``PHY_BBConfig8188F`` (phycfg.c:612-660): BB/RF
enable, RF reset dance, the PHY_REG + AGC tables, crystal cap. The MP
para-file block compiles in (MP_DRIVER=1) but needs ``mp_mode==1`` (registry
default 0).
"""
from __future__ import annotations

import time

from . import bb_agc_tab_tbl, bb_phy_reg_tbl, phy_cond, rf
from . import constants as C

MASK_DWORD = 0xFFFFFFFF


def BIT(n: int) -> int:
    return 1 << n


def bit_shift(mask: int) -> int:
    for i in range(32):
        if (mask >> i) & 0x1:
            return i
    return 32


def query_bb_reg(t, addr: int, mask: int) -> int:
    return (t.read32(addr) & mask) >> bit_shift(mask)


def set_bb_reg(t, addr: int, mask: int, data: int) -> None:
    if mask != MASK_DWORD:
        original = t.read32(addr)
        data = (original & ~mask) | ((data << bit_shift(mask)) & mask)
    t.write32(addr, data)


def set_mac_reg(t, addr: int, mask: int, data: int) -> None:
    set_bb_reg(t, addr, mask, data)


def _config_phy_reg(t, addr: int, value: int) -> None:
    if addr == 0xFE:
        time.sleep(0.050)
    elif addr == 0xFD:
        time.sleep(0.005)
    elif addr == 0xFC:
        time.sleep(0.001)
    elif addr == 0xFB:
        time.sleep(50e-6)
    elif addr == 0xFA:
        time.sleep(5e-6)
    elif addr == 0xF9:
        time.sleep(1e-6)
    else:
        set_bb_reg(t, addr, MASK_DWORD, value)
    time.sleep(1e-6)


def set_crystal_cap(t, crystal_cap: int) -> None:
    crystal_cap &= 0x3F
    set_bb_reg(t, C.REG_AFE_XTAL_CTRL, 0x007FF800,
               crystal_cap | (crystal_cap << 6))


def bb_config(t, crystal_cap: int, mp_mode: int = 0) -> None:
    reg_val = t.read16(C.REG_SYS_FUNC_EN)
    t.write16(C.REG_SYS_FUNC_EN, reg_val | BIT(13) | BIT(0) | BIT(1))
    t.write8(C.REG_RF_CTRL, C.RF_EN | C.RF_RSTB | C.RF_SDMRSTB)
    time.sleep(10e-6)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x1, rf.RF_REG_OFFSET_MASK, 0x780)
    t.write8(C.REG_SYS_FUNC_EN,
             C.FEN_USBD | C.FEN_USBA | C.FEN_BB_GLB_RSTn | C.FEN_BBRSTB)
    phy_cond.walk_table(bb_phy_reg_tbl.TABLE, lambda a, v: _config_phy_reg(t, a, v))
    if mp_mode == 1:
        # TODO: verify, untested here, needs mp_mode=1
        raise ValueError("MP PHY_REG table untested here")
    phy_cond.walk_table(bb_agc_tab_tbl.TABLE,
                        lambda a, v: (set_bb_reg(t, a, MASK_DWORD, v),
                                      time.sleep(1e-6)))
    set_crystal_cap(t, crystal_cap)
