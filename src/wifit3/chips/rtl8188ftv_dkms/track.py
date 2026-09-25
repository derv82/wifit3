"""RTL8188FTV DKMS thermal tracking callback (MIX_MODE) + RA retry count.

Ported from ``odm_TXPowerTrackingCheckCE`` (hal/phydm/phydm_powertracking_ce.c:
TM_Trigger toggle), ``ODM_TXPowerTrackingCallback_ThermalMeter``
(hal/phydm/halphyrf_ce.c) with ``ODM_TxPwrTrackSetPwr_8188F`` MIX_MODE,
``setIqkMatrix_8188F`` and ``GetDeltaSwingTable_8188F``
(hal/phydm/rtl8188f/halphyrf_8188f.c), plus ``phydm_NoisyDetection``
(hal/phydm/phydm.c) feeding ``phydm_ra_dynamic_retry_count``
(hal/phydm/phydm_rainfo.c). 1T1R keeps path A only; with no TX in monitor
mode ``pDM_Odm->TxRate`` stays 0, ``HwRateToMRate(0)`` is MGN_1M, so the CCK
delta pair (MP USB ``2GCCKA``) feeds Absolute and the OFDM remnant never
leaves 0 in either capture. LCK/DoIQK/DPK never fire here (deltas stay
under Threshold_IQK 8, DpkThermal is zero); base/index bookkeeping is sw-only.
"""
from __future__ import annotations

from . import bb
from . import bb_swing_tbl
from . import rf
from . import rf_txpwr_track_tbl
from . import txpower as txpower_mod

DEFAULT_OFDM_INDEX = 28
DEFAULT_CCK_INDEX = 20
PWR_TRACK_LIMIT_OFDM = 34
AVG_THERMAL_NUM = 4
TRACK_TABLE_SIZE = 30
OFDM_TABLE_SIZE = 43
CCK_TABLE_SIZE_88F = 21
THRESHOLD_IQK = 8
TX_RATE_1M = 0x02


def BIT(n: int) -> int:
    return 1 << n


def thermal_trigger(t) -> None:
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x42, BIT(17) | BIT(16), 0x03)


def thermal_read(t) -> int:
    return rf.query_rf_reg(t, rf.RF_PATH_A, 0x42, 0xFC00)


def kfree_gain_offset(t, offset: int = 0) -> None:
    write_value = abs(offset) | (BIT(5) if offset > 0 else 0)
    rf.set_rf_reg(t, rf.RF_PATH_A, 0x55, 0x0FC000, write_value)


def tracking_init_state(eeprom_thermal: int) -> dict:
    return {
        "th_avg": [0] * AVG_THERMAL_NUM,
        "th_avg_idx": 0,
        "th_val": eeprom_thermal,
        "th_lck": eeprom_thermal,
        "th_iqk": eeprom_thermal,
        "dlt": 0,
        "dlt_last": 0,
        "abs_ofdm": 0,
        "rem_ofdm": 0,
        "rem_cck": 0,
        "mod_ofdm": False,
        "mod_cck": False,
        "mod_val_ofdm": 0,
        "mod_val_cck": 0,
        "noisy_smooth": 0,
        "pre_noisy": False,
        "track_control": True,
        "eeprom_thermal": eeprom_thermal,
    }


def delta_tables(tx_rate: int, channel: int) -> tuple[list, list]:
    if not 1 <= channel <= 14:
        raise ValueError(f"5 GHz unreachable on this silicon: ch{channel}")
    if tx_rate in txpower_mod.CCK_RATES:
        return (rf_txpwr_track_tbl.TABLES["2GCCKA_P"],
                rf_txpwr_track_tbl.TABLES["2GCCKA_N"])
    return (rf_txpwr_track_tbl.TABLES["2GA_P"],
            rf_txpwr_track_tbl.TABLES["2GA_N"])


def set_iqk_matrix(t, final_ofdm: int, x: int, y: int) -> None:
    idx = min(max(final_ofdm, 0), OFDM_TABLE_SIZE - 1)
    ele_d = (bb_swing_tbl.OFDM_SWING_NEW[idx] & 0xFFC00000) >> 22
    if x != 0:
        if x & 0x200:
            x |= 0xFFFFFC00
        if y & 0x200:
            y |= 0xFFFFFC00
        ele_a = ((x * ele_d) >> 8) & 0x3FF
        ele_c = ((y * ele_d) >> 8) & 0x3FF
        bb.set_bb_reg(t, 0xC80, 0xFFFFFFFF,
                      (ele_d << 22) | ((ele_c & 0x3F) << 16) | ele_a)
        bb.set_bb_reg(t, 0xC94, 0xF0000000, (ele_c & 0x3C0) >> 6)
        bb.set_bb_reg(t, 0xC4C, BIT(24), ((x * ele_d) >> 7) & 0x1)
    else:
        bb.set_bb_reg(t, 0xC80, 0xFFFFFFFF,
                      bb_swing_tbl.OFDM_SWING_NEW[idx])
        bb.set_bb_reg(t, 0xC94, 0xF0000000, 0x00)
        bb.set_bb_reg(t, 0xC4C, BIT(24), 0x00)


_CCK_SWING_ADDRS = (0xA22, 0xA23, 0xA24, 0xA25, 0xA26, 0xA27, 0xA28,
                    0xA29, 0xA9A, 0xA9B, 0xA9C, 0xA9D, 0xAA0, 0xAA1,
                    0xAA2, 0xAA3)


def write_cck_swing(t, index: int) -> None:
    row = bb_swing_tbl.CCK_SWING_88F[index]
    for addr, value in zip(_CCK_SWING_ADDRS, row):
        t.write8(addr, value)


def set_pwr_mix(t, st: dict, channel: int, params, by_rate) -> None:
    final_ofdm = DEFAULT_OFDM_INDEX + st["abs_ofdm"]
    final_cck = DEFAULT_CCK_INDEX + st["abs_ofdm"]
    if final_ofdm > PWR_TRACK_LIMIT_OFDM:
        st["rem_ofdm"] = final_ofdm - PWR_TRACK_LIMIT_OFDM
        set_iqk_matrix(t, PWR_TRACK_LIMIT_OFDM, st["iqk_x"], st["iqk_y"])
        st["mod_ofdm"] = True
    elif final_ofdm < DEFAULT_OFDM_INDEX:
        st["rem_ofdm"] = final_ofdm - DEFAULT_OFDM_INDEX
        set_iqk_matrix(t, DEFAULT_OFDM_INDEX, st["iqk_x"], st["iqk_y"])
        st["mod_ofdm"] = True
    else:
        set_iqk_matrix(t, final_ofdm, st["iqk_x"], st["iqk_y"])
        if st["mod_ofdm"]:
            st["rem_ofdm"] = 0
    txpower_mod.set_section(t, channel, 0, params, by_rate,
                            txpower_mod.OFDM_RATES, st["rem_cck"],
                            st["rem_ofdm"])
    txpower_mod.set_section(t, channel, 0, params, by_rate,
                            txpower_mod.MCS07_RATES, st["rem_cck"],
                            st["rem_ofdm"])
    st["mod_val_ofdm"] = st["rem_ofdm"]
    if final_cck > CCK_TABLE_SIZE_88F - 1:
        st["rem_cck"] = final_cck - (CCK_TABLE_SIZE_88F - 1)
        write_cck_swing(t, CCK_TABLE_SIZE_88F - 1)
        st["mod_cck"] = True
    elif final_cck < 0:
        st["rem_cck"] = final_cck
        write_cck_swing(t, 0)
        st["mod_cck"] = True
    else:
        write_cck_swing(t, final_cck)
        st["mod_cck"] = False
        st["rem_cck"] = 0
    txpower_mod.set_section(t, channel, 0, params, by_rate,
                            txpower_mod.CCK_RATES, st["rem_cck"],
                            st["rem_ofdm"])
    st["mod_val_cck"] = st["rem_cck"]


def tracking_callback(t, st: dict, channel: int, params, by_rate) -> bool:
    raw = thermal_read(t)
    if (not st["track_control"] or st["eeprom_thermal"] == 0
            or st["eeprom_thermal"] == 0xFF):
        return False
    st["th_avg"][st["th_avg_idx"]] = raw
    st["th_avg_idx"] = (st["th_avg_idx"] + 1) % AVG_THERMAL_NUM
    nz = [v for v in st["th_avg"] if v]
    probe = sum(nz) // len(nz)
    delta = abs(probe - st["th_val"])
    delta_iqk = abs(probe - st["th_iqk"])
    if st["th_lck"] == 0xFF:
        raise ValueError("no-PG LCK path untested here")
    if delta_iqk >= THRESHOLD_IQK:
        raise ValueError("thermal DoIQK path untested here")
    if delta > 0 and st["track_control"]:
        table_delta = abs(probe - st["eeprom_thermal"])
        if table_delta >= TRACK_TABLE_SIZE:
            table_delta = TRACK_TABLE_SIZE - 1
        tup, tdown = delta_tables(TX_RATE_1M, channel)
        if probe > st["eeprom_thermal"]:
            st["dlt"] = tup[table_delta]
        else:
            st["dlt"] = -tdown[table_delta]
        offset = st["dlt"] - st["dlt_last"]
        st["dlt_last"] = st["dlt"]
        st["abs_ofdm"] = st["dlt"]
        if offset != 0 and st["eeprom_thermal"] != 0xFF:
            set_pwr_mix(t, st, channel, params, by_rate)
            st["th_val"] = probe
            return True
    return False


def noisy_score(fa: dict, st: dict) -> tuple[int, int]:
    total_fa, total_cca = fa["all"], fa["cca"]
    score = 0
    for i in range(17):
        if total_fa * 16 >= total_cca * (16 - i):
            score = 16 - i
            break
    st["noisy_smooth"] = (st["noisy_smooth"] >> 1) + (score << 2)
    smooth = ((st["noisy_smooth"] + 3) >> 3) if total_cca >= 300 else 0
    return score, smooth


def ra_dynamic_retry_count(t, st: dict, fa: dict) -> None:
    _, smooth = noisy_score(fa, st)
    noisy = smooth >= 3
    if st["pre_noisy"] != noisy:
        if noisy:
            t.write32(0x430, 0x0)
            t.write32(0x434, 0x04030201)
        else:
            t.write32(0x430, 0x02010000)
            t.write32(0x434, 0x06050403)
        st["pre_noisy"] = noisy
