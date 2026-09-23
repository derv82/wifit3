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
from . import track as track_mod
from . import misc as misc_mod


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


FA_HOLDC = 0xC00
FA_RSTD = 0xD00
FA_TYPE1 = 0xCF0
FA_TYPE2 = 0xDA0
FA_TYPE3 = 0xDA4
FA_TYPE4 = 0xDA8
FA_RSTC = 0xC0C
FA_CCK_RST = 0xA2C
FA_CCK_LSB = 0xA5C
FA_CCK_MSB = 0xA58
FA_CCK_CCA = 0xA60

DIG_MIN = 0x1E
DIG_MAX_OF_MIN = 0x2A
DIG_FA_THRES = (2000, 4000, 5000)


def false_alarm_stats(t) -> dict:
    bb.set_bb_reg(t, FA_HOLDC, BIT(31), 1)
    bb.set_bb_reg(t, FA_RSTD, BIT(31), 1)
    v = t.read32(FA_TYPE1)
    fast, sb = v & 0xFFFF, (v >> 16) & 0xFFFF
    v = t.read32(FA_TYPE2)
    cca, parity = v & 0xFFFF, (v >> 16) & 0xFFFF
    v = t.read32(FA_TYPE3)
    rate, crc8 = v & 0xFFFF, (v >> 16) & 0xFFFF
    mcs = t.read32(FA_TYPE4) & 0xFFFF
    bb.set_bb_reg(t, FA_CCK_RST, BIT(12), 1)
    bb.set_bb_reg(t, FA_CCK_RST, BIT(14), 1)
    cck_fail = bb.query_bb_reg(t, FA_CCK_LSB, 0xFF)
    cck_fail += bb.query_bb_reg(t, FA_CCK_MSB, 0xFF000000) << 8
    v = t.read32(FA_CCK_CCA)
    cck_cca = ((v & 0xFF) << 8) | ((v & 0xFF00) >> 8)
    fa = {"all": fast + sb + parity + rate + crc8 + mcs + cck_fail,
          "cck_fail": cck_fail, "cca": cca + cck_cca}
    bb.set_bb_reg(t, FA_RSTC, BIT(31), 1)
    bb.set_bb_reg(t, FA_RSTC, BIT(31), 0)
    bb.set_bb_reg(t, FA_RSTD, BIT(27), 1)
    bb.set_bb_reg(t, FA_RSTD, BIT(27), 0)
    bb.set_bb_reg(t, FA_HOLDC, BIT(31), 0)
    bb.set_bb_reg(t, FA_RSTD, BIT(31), 0)
    bb.set_bb_reg(t, FA_CCK_RST, BIT(13) | BIT(12), 0)
    bb.set_bb_reg(t, FA_CCK_RST, BIT(13) | BIT(12), 2)
    bb.set_bb_reg(t, FA_CCK_RST, BIT(15) | BIT(14), 0)
    bb.set_bb_reg(t, FA_CCK_RST, BIT(15) | BIT(14), 2)
    return fa


def dig_step(t, st: dict, fa: dict) -> None:
    cur = st["cur_ig"]
    if fa["all"] > DIG_FA_THRES[2]:
        cur += 4
    elif fa["all"] > DIG_FA_THRES[1]:
        cur += 2
    elif fa["all"] < DIG_FA_THRES[0]:
        cur -= 2
    cur = max(DIG_MIN, min(DIG_MAX_OF_MIN, cur))
    if cur != st["cur_ig"]:
        bb.set_bb_reg(t, 0xC50, 0xFF, cur)
        st["cur_ig"] = cur


def adaptivity_edcca(t, st: dict) -> None:
    if not st["adaptivity_ability"]:
        st["th_l2h_ini"] = 20
        st["th_hl_diff"] = 8
    th_l2h = (st["th_l2h_ini"] + 0x32) & 0xFF
    th_h2l = (th_l2h - st["th_hl_diff"]) & 0xFF
    bb.set_bb_reg(t, 0xC4C, 0xFF00FF, th_l2h | (th_h2l << 16))


def cck_pd(t, st: dict, fa: dict) -> None:
    thres = 0x83 if fa["cck_fail"] > 1000 else 0x40
    if thres != st["cur_cck"]:
        t.write8(0xA0A, thres)
        st["cur_cck"] = thres


def watchdog_tick(t, st: dict, trigger: bool) -> dict:
    misc_mod.check_rxfifo_full(t)
    fa = false_alarm_stats(t)
    dig_step(t, st, fa)
    adaptivity_edcca(t, st)
    cck_pd(t, st, fa)
    if trigger:
        track_mod.thermal_trigger(t)
    else:
        track_mod.thermal_read(t)
    return fa
