"""RTL8188FTV DKMS Path-A IQK (M5h).

Ported from ``PHY_IQCalibrate_8188F`` + ``phy_IQCalibrate_8188F`` +
``phy_PathA_IQK_8188F`` / ``phy_PathA_RxIQK8188F`` +
``_PHY_PathAFillIQKMatrix8188F`` + the Save/Reload/MACSetting/PathADDAOn
helpers (hal/phydm/rtl8188f/halphyrf_8188f.c). Recorded 1T1R path only:
Path B, MP gates and the bReCovery/bRestore entry are omitted. TRACE
macros compile out (no 0xE90/0xE98/0xEA0/0xEA8 monitor reads on the wire).
"""
from __future__ import annotations

import time

from . import bb, rf

ADDA_REG = [0x85C, 0xE6C, 0xE70, 0xE74, 0xE78, 0xE7C, 0xE80, 0xE84,
            0xE88, 0xE8C, 0xED0, 0xED4, 0xED8, 0xEDC, 0xEE0, 0xEEC]
IQK_MAC_REG = [(0x522, 1), (0x550, 1), (0x551, 1), (0x40, 4)]
IQK_BB_REG = [0xC04, 0xC08, 0x874, 0xB68, 0xB6C, 0x870, 0x860, 0x864, 0x800]
IQK_BB_RECOVER = [0xC14, 0xC1C, 0xC4C, 0xC78, 0xC80, 0xC88, 0xC94, 0xC9C, 0xCA0]

_RF_WE_LUT = 0xEF
_RF_RCK_OS = 0x30
_RF_TXPA_G1 = 0x31
_RF_TXPA_G2 = 0x32
_MAX_TOLERANCE = 5
_ONE_SHOT_DELAY = 0.025


def save_adda(t, st: dict) -> None:
    st["adda"] = [t.read32(a) for a in ADDA_REG]


def save_mac(t, st: dict) -> None:
    st["mac"] = [t.read8(0x522), t.read8(0x550), t.read8(0x551),
                 t.read32(0x40)]


def save_bb(t, st: dict) -> None:
    st["bb"] = [t.read32(a) for a in IQK_BB_REG]


def path_adda_on(t) -> None:
    for a in ADDA_REG:
        t.write32(a, 0x03C00014)


def mac_setting_calibration(t) -> None:
    bb.set_bb_reg(t, 0x520, 0x00FF0000, 0xFF)


def reload_adda(t, st: dict) -> None:
    for a, v in zip(ADDA_REG, st["adda"]):
        t.write32(a, v)


def reload_mac(t, st: dict) -> None:
    t.write8(0x522, st["mac"][0])
    t.write8(0x550, st["mac"][1])
    t.write8(0x551, st["mac"][2])
    t.write32(0x40, st["mac"][3])


def reload_bb(t, st: dict) -> None:
    for a, v in zip(IQK_BB_REG, st["bb"]):
        t.write32(a, v)


def _rf_standby(t, rck: int, g1: int, g2: int, v56: int) -> None:
    rf.set_rf_reg(t, rf.RF_PATH_A, _RF_WE_LUT, 0x80000, 0x1)
    rf.set_rf_reg(t, rf.RF_PATH_A, _RF_RCK_OS, rf.RF_REG_OFFSET_MASK, rck)
    rf.set_rf_reg(t, rf.RF_PATH_A, _RF_TXPA_G1, rf.RF_REG_OFFSET_MASK, g1)
    rf.set_rf_reg(t, rf.RF_PATH_A, _RF_TXPA_G2, rf.RF_REG_OFFSET_MASK, g2)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0xDF, rf.RF_REG_OFFSET_MASK, 0x980)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x56, rf.RF_REG_OFFSET_MASK, v56)


def _one_shot(t) -> None:
    t.write32(0xE48, 0xF9000000)
    t.write32(0xE48, 0xF8000000)
    time.sleep(_ONE_SHOT_DELAY)
    bb.set_bb_reg(t, 0xE28, 0xFFFFFF00, 0x000000)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0xDF, rf.RF_REG_OFFSET_MASK, 0x180)


def path_a_tx_iqk(t) -> tuple[int, int]:
    bb.set_bb_reg(t, 0xE28, 0xFFFFFF00, 0x000000)
    _rf_standby(t, 0x20000, 0xF, 0x7FF7, 0x5102A)
    bb.set_bb_reg(t, 0xE28, 0xFFFFFF00, 0x808000)
    t.write32(0xE30, 0x18008C1C)
    t.write32(0xE34, 0x38008C1C)
    t.write32(0xE38, 0x821403FF)
    t.write32(0xE3C, 0x28160000)
    t.write32(0xE4C, 0x00462911)
    _one_shot(t)
    lok = rf.query_rf_reg(t, rf.RF_PATH_A, 0x8, rf.RF_REG_OFFSET_MASK)
    eac = t.read32(0xEAC)
    e94 = t.read32(0xE94)
    e9c = t.read32(0xE9C)
    ok = (not eac & 0x10000000
          and ((e94 & 0x03FF0000) >> 16) != 0x142
          and ((e9c & 0x03FF0000) >> 16) != 0x42)
    return (0x01 if ok else 0x00), lok


def path_a_rx_iqk(t, lok: int) -> int:
    bb.set_bb_reg(t, 0xE28, 0xFFFFFF00, 0x000000)
    _rf_standby(t, 0x30000, 0xF, 0xF1173, 0x5102A)
    bb.set_bb_reg(t, 0xE28, 0xFFFFFF00, 0x808000)
    t.write32(0xE40, 0x01007C00)
    t.write32(0xE44, 0x01004800)
    t.write32(0xE30, 0x10008C1C)
    t.write32(0xE34, 0x30008C1C)
    t.write32(0xE38, 0x82160FFF)
    t.write32(0xE3C, 0x28160000)
    t.write32(0xE4C, 0x00462911)
    _one_shot(t)
    eac = t.read32(0xEAC)
    e94 = t.read32(0xE94)
    e9c = t.read32(0xE9C)
    if not (not eac & 0x10000000
            and ((e94 & 0x03FF0000) >> 16) != 0x142
            and ((e9c & 0x03FF0000) >> 16) != 0x42):
        return 0x00
    t.write32(0xE40, 0x80007C00 | (e94 & 0x3FF0000) | ((e9c & 0x3FF0000) >> 16))
    bb.set_bb_reg(t, 0xE28, 0xFFFFFF00, 0x000000)
    _rf_standby(t, 0x30000, 0xF, 0xF7FF2, 0x51000)
    bb.set_bb_reg(t, 0xE28, 0xFFFFFF00, 0x808000)
    t.write32(0xE44, 0x01004800)
    t.write32(0xE30, 0x30008C1C)
    t.write32(0xE34, 0x10008C1C)
    t.write32(0xE38, 0x82160000)
    t.write32(0xE3C, 0x281613FF)
    t.write32(0xE4C, 0x0046A911)
    _one_shot(t)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x8, rf.RF_REG_OFFSET_MASK, lok)
    eac = t.read32(0xEAC)
    ea4 = t.read32(0xEA4)
    ok = (not eac & 0x08000000
          and ((ea4 & 0x03FF0000) >> 16) != 0x132
          and ((eac & 0x03FF0000) >> 16) != 0x36)
    return 0x03 if ok else 0x01


def _tx_part_ok(eac: int, e94: int, e9c: int) -> bool:
    return (not eac & 0x10000000
            and ((e94 & 0x03FF0000) >> 16) != 0x142
            and ((e9c & 0x03FF0000) >> 16) != 0x42)


def fill_path_a_matrix(t, st: dict, row: list[int]) -> None:
    old = (bb.query_bb_reg(t, 0xC80, 0xFFFFFFFF) >> 22) & 0x3FF
    x = row[0]
    tx0_a = (x * old) >> 8
    bb.set_bb_reg(t, 0xC80, 0x3FF, tx0_a)
    bb.set_bb_reg(t, 0xC4C, 0x80000000, (x * old >> 7) & 0x1)
    y = row[1]
    tx0_c = (y * old) >> 8
    bb.set_bb_reg(t, 0xC94, 0xF0000000, (tx0_c & 0x3C0) >> 6)
    st["txiqc_c94"] = t.read32(0xC94)
    bb.set_bb_reg(t, 0xC80, 0x003F0000, tx0_c & 0x3F)
    st["txiqc_c80"] = t.read32(0xC80)
    bb.set_bb_reg(t, 0xC4C, 0x20000000, (y * old >> 7) & 0x1)
    st["txiqc_c4c"] = t.read32(0xC4C)
    reg = row[2]
    bb.set_bb_reg(t, 0xC14, 0x3FF, reg)
    reg = row[3] & 0x3F
    bb.set_bb_reg(t, 0xC14, 0xFC00, reg)
    st["rxiqc_c14"] = t.read32(0xC14)
    reg = (row[3] >> 6) & 0xF
    bb.set_bb_reg(t, 0xCA0, 0xF0000000, reg)
    st["rxiqc_ca0"] = t.read32(0xCA0)


def fill_path_a_matrix_txonly(t, st: dict) -> None:
    st["rxiqc_ca0"] = t.read32(0xCA0)
    st["rxiqc_c14"] = t.read32(0xC14)


def _signed(v: int) -> int:
    return v - 0x100000000 if v & 0x80000000 else v


def similarity(result: list[list[int]], c1: int, c2: int) -> bool:
    final_candidate = [0xFF, 0xFF]
    hit = True
    bitmap = 0
    for i in range(8):
        if i in (1, 3, 5, 7):
            a = _signed(c1v | 0xFFFFFC00) if (c1v := result[c1][i]) & 0x200 else c1v
            b = _signed(c2v | 0xFFFFFC00) if (c2v := result[c2][i]) & 0x200 else c2v
        else:
            a, b = result[c1][i], result[c2][i]
        if abs(a - b) > _MAX_TOLERANCE:
            if i in (2, 6) and not bitmap:
                if result[c1][i] + result[c1][i + 1] == 0:
                    final_candidate[i // 4] = c2
                elif result[c2][i] + result[c2][i + 1] == 0:
                    final_candidate[i // 4] = c1
                else:
                    bitmap |= 1 << i
            else:
                bitmap |= 1 << i
    if bitmap == 0:
        for i in range(2):
            if final_candidate[i] != 0xFF:
                for j in range(i * 4, (i + 1) * 4 - 2):
                    result[3][j] = result[final_candidate[i]][j]
                hit = False
        return hit
    if not bitmap & 0x03:
        result[3][0:2] = result[c1][0:2]
    if not bitmap & 0x0C:
        result[3][2:4] = result[c1][2:4]
    if not bitmap & 0x30:
        result[3][4:6] = result[c1][4:6]
    if not bitmap & 0xC0:
        result[3][6:8] = result[c1][6:8]
    return False


def worker(t, st: dict, result: list[list[int]], idx: int) -> None:
    st["tmp_c50"] = bb.query_bb_reg(t, 0xC50, 0xFF)
    st["tmp_c58"] = bb.query_bb_reg(t, 0xC58, 0xFF)
    if idx == 0:
        save_adda(t, st)
        save_mac(t, st)
        save_bb(t, st)
    path_adda_on(t)
    if idx == 0:
        st["pi_enable"] = bb.query_bb_reg(t, 0x820, 0x100)
    st["path_sel_bb"] = t.read32(0x948)
    st["path_sel_rf"] = rf.query_rf_reg(t, rf.RF_PATH_A, 0xB0,
                                       rf.RF_REG_OFFSET_MASK)
    bb.set_bb_reg(t, 0xC04, 0xFFFFFFFF, 0x03A05600)
    bb.set_bb_reg(t, 0xC08, 0xFFFFFFFF, 0x000800E4)
    bb.set_bb_reg(t, 0x874, 0xFFFFFFFF, 0x25204000)
    mac_setting_calibration(t)
    bb.set_bb_reg(t, 0xE28, 0xFFFFFF00, 0x808000)
    t.write32(0xE40, 0x01007C00)
    t.write32(0xE44, 0x01004800)
    for _ in range(2):
        ok, lok = path_a_tx_iqk(t)
        if ok == 0x01:
            bb.set_bb_reg(t, 0xE28, 0xFFFFFF00, 0x000000)
            st["txlok"] = rf.query_rf_reg(t, rf.RF_PATH_A, 0x8,
                                         rf.RF_REG_OFFSET_MASK)
            result[idx][0] = (t.read32(0xE94) & 0x03FF0000) >> 16
            result[idx][1] = (t.read32(0xE9C) & 0x03FF0000) >> 16
            break
    for _ in range(2):
        if path_a_rx_iqk(t, st.get("txlok", lok)) == 0x03:
            result[idx][2] = (t.read32(0xEA4) & 0x03FF0000) >> 16
            result[idx][3] = (t.read32(0xEAC) & 0x03FF0000) >> 16
            break
    bb.set_bb_reg(t, 0xE28, 0xFFFFFF00, 0x000000)
    if idx != 0:
        reload_adda(t, st)
        reload_mac(t, st)
        reload_bb(t, st)
        t.write32(0x948, st["path_sel_bb"])
        rf.set_rf_reg(t, rf.RF_PATH_A, 0xB0, rf.RF_REG_OFFSET_MASK,
                      st["path_sel_rf"])
        bb.set_bb_reg(t, 0xC50, 0xFF, 0x50)
        bb.set_bb_reg(t, 0xC50, 0xFF, st["tmp_c50"])
        t.write32(0xE30, 0x01008C00)
        t.write32(0xE34, 0x01008C00)


def iq_calibrate(t) -> dict:
    st: dict = {}
    result = [[0] * 8 for _ in range(4)]
    st["wrapper_sel_bb"] = t.read32(0x948)
    st["wrapper_sel_rf"] = rf.query_rf_reg(t, rf.RF_PATH_A, 0xB0,
                                          rf.RF_REG_OFFSET_MASK)
    final = 0xFF
    for i in range(3):
        worker(t, st, result, i)
        if i == 1 and similarity(result, 0, 1):
            final = 0
            break
        if i == 2:
            if similarity(result, 0, 2):
                final = 0
                break
            if similarity(result, 1, 2):
                final = 1
            elif sum(result[3]) != 0:
                final = 3
    if final != 0xFF and result[final][0] != 0:
        if result[final][2] == 0:
            fill_path_a_matrix_txonly(t, st)
        else:
            fill_path_a_matrix(t, st, result[final])
    st["bb_recover"] = [t.read32(a) for a in IQK_BB_RECOVER]
    t.write32(0x948, st["wrapper_sel_bb"])
    rf.set_rf_reg(t, rf.RF_PATH_A, 0xB0, rf.RF_REG_OFFSET_MASK,
                  st["wrapper_sel_rf"])
    st["result"] = result
    st["final"] = final
    return st
