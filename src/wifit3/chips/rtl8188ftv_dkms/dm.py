"""RTL8188FTV DKMS DM-init prologue (M5h).

Ported from ``odm_CommonInfoSelfInit`` (hal/phydm/phydm.c:228-245: CCK report
+ RX-path reads), ``odm_DIGInit`` IGI read (hal/phydm/phydm_dig.c:691+),
``Phydm_NHMCounterStatisticsInit`` 11N branch
(hal/phydm/phydm_adaptivity.c:138-155), ``Phydm_AdaptivityInit`` wire part
(phydm_adaptivity.c:654-667: MACEDCCA + DBG_RPT + EDCCA_DCNF),
``ODM_CfoTrackingInit`` ATC read (phydm_cfotracking.c) and
``odm_TXPowerTrackingThermalMeterInit`` swing read
(phydm_powertracking_ce.c:439+). Everything else in these inits is sw state.
"""
from __future__ import annotations

from . import bb


def BIT(n: int) -> int:
    return 1 << n


def common_info_self_init(t) -> tuple[int, int]:
    cck_high_power = bb.query_bb_reg(t, 0x824, BIT(9))
    rf_path_rx = bb.query_bb_reg(t, 0xC04, 0xF)
    return cck_high_power, rf_path_rx


def dig_init_igi(t) -> int:
    return bb.query_bb_reg(t, 0xC50, 0xFFFFFFFF)


def nhm_init(t) -> None:
    t.write16(0x896, 0xC350)
    t.write16(0x892, 0xFFFF)
    t.write32(0x898, 0xFFFFFF50)
    t.write32(0x89C, 0xFFFFFFFF)
    bb.set_bb_reg(t, 0xE28, 0xFF, 0xFF)
    bb.set_bb_reg(t, 0x890, BIT(10) | BIT(9) | BIT(8), 0x1)
    bb.set_bb_reg(t, 0xC0C, BIT(7), 0x1)


def adaptivity_init(t) -> None:
    bb.set_bb_reg(t, 0x520, BIT(15), 0)
    bb.set_bb_reg(t, 0x524, BIT(11), 1)
    bb.set_bb_reg(t, 0x908, 0xFFFFFFFF, 0x208)
    bb.set_bb_reg(t, 0xE24, BIT(21) | BIT(20), 0x1)


def cfo_init_atc(t) -> int:
    return bb.query_bb_reg(t, 0xD2C, BIT(11))


def thermal_swing_index(t) -> int:
    return bb.query_bb_reg(t, 0xC80, 0xFFC00000)


def dm_init(t) -> None:
    common_info_self_init(t)
    dig_init_igi(t)
    nhm_init(t)
    adaptivity_init(t)
    cfo_init_atc(t)
    thermal_swing_index(t)
