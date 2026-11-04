"""RTL8812AU dynamic-check watchdog: rtw_dynamic_chk_wk_hdl (rtw_cmd.c:2993), the 2 s cmd-thread work.

The always-monitor case: never linked, so rssi_min stays 0 and no station entry exists.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..rtl88xxau_base.sipi import (
    RF_CHNLBW, RF_PATH_A, RF_PATH_B, RFREG_WRITE_MASK, query_bb, query_rf, set_bb, set_rf_reg,
)
from .efuse import ChipParams
from .txpower import phy_set_tx_power_level_by_path
from .txpwr_track_tbl import (
    DELTA_SWING_TABLE_IDX_2GA_N_8188E, DELTA_SWING_TABLE_IDX_2GA_P_8188E, TXPOWERTRACK_RFE3_8812A,
    TXPOWERTRACK_USB_8812A, DeltaSwingTables,
)

DYNAMIC_CHK_TIMER_PERIOD_S = 2.0    # _set_timer(&pdvobj->dynamic_chk_timer, 2000) rtw_mlme.c:4136

REG_TXDMA_STATUS = 0x0210
REG_RXDMA_STATUS = 0x0288
REG_RCR = 0x0608
REG_TXPAUSE = 0x0522

ODM_REG_BB_RX_PATH_11AC = 0x0808
ODM_REG_CCK_FA_11AC = 0x0A5C
ODM_REG_IGI_A_11AC = 0x0C50
ODM_REG_IGI_B_11AC = 0x0E50
ODM_BIT_IGI_11AC = 0x7F
ODM_REG_CCK_CRC32_CNT_11AC = 0x0F04
ODM_REG_CCK_CCA_CNT_11AC = 0x0F08
ODM_REG_VHT_CRC32_CNT_11AC = 0x0F0C
ODM_REG_HT_CRC32_CNT_11AC = 0x0F10
ODM_REG_OFDM_CRC32_CNT_11AC = 0x0F14
ODM_REG_OFDM_FA_TYPE1_11AC = 0x0FCC
ODM_REG_OFDM_FA_TYPE2_11AC = 0x0FD0
ODM_REG_OFDM_FA_TYPE3_11AC = 0x0FBC
ODM_REG_OFDM_FA_TYPE4_11AC = 0x0FC0
ODM_REG_OFDM_FA_TYPE5_11AC = 0x0FC4
ODM_REG_OFDM_FA_TYPE6_11AC = 0x0FC8
REG_A_TX_SCALE_JAGUAR = 0x0C1C
REG_B_TX_SCALE_JAGUAR = 0x0E1C
REG_SINGLE_TONE_CONT_TX_JAGUAR = 0x0914
RF_T_METER_8812A = 0x42
RF_LCK = 0xB4

# phydm_ccx.h
CCA_CAP = 14
NHM_PERIOD_MAX = 65534
CLM_PERIOD_MAX = 65535
NHM_CNT_ALL = 0
NHM_CNT_INIT = 2
NHM_EXCLUDE_CCA = 0
NHM_CCA_INIT = 2
NHM_EXCLUDE_TXON = 0
NHM_TXON_INIT = 2

# phydm_dig.h
DIG_MAX_COVERAGR = 0x26
DIG_MIN_COVERAGE = 0x1C
DIG_MAX_OF_MIN_BALANCE_MODE = 0x2A

# phydm_cck_pd.h
CCK_FA_MA_RESET = 0xFFFFFFFF
CCK_PD_LV_INIT = 0xFF
CCK_PD_LV_0 = 0
CCK_PD_LV_1 = 1

# phydm_adaptivity.h
EDCCA_HL_DIFF_NORMAL = 8
ADAPT_DC_BACKOFF = 2

# phydm_pre_define.h
ODM_RATE6M = 0x04
ODM_RATEMCS0 = 0x0C
ODM_RATEVHTSS1MCS0 = 0x2C
PHYDM_SPEC_RATE_6M = 0xB

# halrf
TXSCALE_TABLE_SIZE = 37
TXPWR_TRACK_TABLE_SIZE = 30
IQK_THRESHOLD = 8
AVG_THERMAL_NUM_8812A = 4
MAX_PATH_NUM_8812A = 2
MGN_1M = 0x02
MGN_2M = 0x04
MGN_5_5M = 0x0B
MGN_6M = 0x0C
MGN_11M = 0x16
MGN_48M = 0x60
MGN_54M = 0x6C

# halrf_powertracking_ce.c:538
tx_scaling_table_jaguar = (
    0x081, 0x088, 0x090, 0x099, 0x0A2, 0x0AC, 0x0B6, 0x0C0, 0x0CC, 0x0D8, 0x0E5, 0x0F2,
    0x101, 0x110, 0x120, 0x131, 0x143, 0x156, 0x16A, 0x180, 0x197, 0x1AF, 0x1C8, 0x1E3,
    0x200, 0x21E, 0x23E, 0x261, 0x285, 0x2AB, 0x2D3, 0x2FE, 0x32B, 0x35C, 0x38E, 0x3C4,
    0x3FE,
)


@dataclass
class PhydmState:
    """The dm_struct members the watchdog carries between fires, seeded by odm_dm_init."""
    cur_ig_value: int = 0
    nhm_igi: int = 0xFF
    nhm_include_txon: int = NHM_TXON_INIT
    nhm_include_cca: int = NHM_CCA_INIT
    nhm_divider_opt: int = NHM_CNT_INIT
    nhm_period: int = 0
    clm_period: int = CLM_PERIOD_MAX
    ofdm2_rate_idx: int = 0
    ht2_rate_idx: int = 0
    vht2_rate_idx: int = 0
    cck_fa_ma: int = CCK_FA_MA_RESET
    cck_pd_lv: int = CCK_PD_LV_1
    h2l_lb: int = 0
    l2h_lb: int = 0
    rrsr_val_init: int = 0
    rrsr_val_curr: int = 0
    # rf_calibrate_info / rf_table, set by phydm_rf_init.
    eeprom_thermal: int = 0xFF
    delta_swing_tables: DeltaSwingTables = TXPOWERTRACK_USB_8812A
    tm_trigger: int = 0
    default_ofdm_index: int = 24
    default_cck_index: int = 24
    bb_swing_diff_2g: int = 0
    bb_swing_diff_5g: int = 0
    thermal_value: int = 0xFF
    thermal_value_lck: int = 0xFF
    thermal_value_iqk: int = 0xFF
    thermal_value_avg: list[int] = field(default_factory=lambda: [0] * AVG_THERMAL_NUM_8812A)
    thermal_value_avg_index: int = 0
    thermal_value_delta: int = 0
    delta_power_index: list[int] = field(default_factory=lambda: [0, 0])
    delta_power_index_last: list[int] = field(default_factory=lambda: [0, 0])
    power_index_offset: list[int] = field(default_factory=lambda: [0, 0])
    absolute_ofdm_swing_idx: list[int] = field(default_factory=lambda: [0, 0])
    remnant_ofdm_swing_idx: list[int] = field(default_factory=lambda: [0, 0])
    remnant_cck_swing_idx: int = 0
    modify_tx_agc_flag_path_a: bool = False
    modify_tx_agc_flag_path_b: bool = False


def s8(value: int) -> int:
    v = value & 0xFF
    if v & 0x80:
        return v - 0x100
    return v


def bb_swing_diff(tx_bb_swing: int) -> int:
    """The dB pRFCalibrateInfo->bb_swing_diff_* phy_get_tx_bb_swing_8812a records with its output."""
    if tx_bb_swing == 0x16A:
        return -3
    if tx_bb_swing == 0x101:
        return -6
    if tx_bb_swing == 0x0B6:
        return -9
    return 0


def phydm_rf_init(st: PhydmState, eeprom_thermal_meter: int, rfe_type: int, tx_bb_swing_2g_a: int) -> None:
    """phydm_rf_init (halphyrf_ce.c:1152): odm_txpowertracking_init + odm_clear_txpowertracking_state."""
    # odm_config_rf_with_tx_pwr_track_header_file (phydm_hwconfig.c:537): rfe 3 on an MP chip.
    if rfe_type == 3:
        st.delta_swing_tables = TXPOWERTRACK_RFE3_8812A
    else:
        st.delta_swing_tables = TXPOWERTRACK_USB_8812A
    st.eeprom_thermal = eeprom_thermal_meter
    # odm_txpowertracking_thermal_meter_init (halrf_powertracking_ce.c:684); get_swing_index calls
    # phy_get_tx_bb_swing_8812a(current band, RF_PATH_A), which records bb_swing_diff_2g.
    st.bb_swing_diff_2g = bb_swing_diff(tx_bb_swing_2g_a)
    if tx_bb_swing_2g_a in tx_scaling_table_jaguar:
        st.default_ofdm_index = tx_scaling_table_jaguar.index(tx_bb_swing_2g_a)
    else:
        st.default_ofdm_index = 24
    st.default_cck_index = 24
    st.thermal_value_iqk = st.eeprom_thermal
    st.thermal_value_lck = st.eeprom_thermal
    st.tm_trigger = 0
    odm_clear_txpowertracking_state(st)


def odm_clear_txpowertracking_state(st: PhydmState) -> None:
    """halphyrf_ce.c:133."""
    for p in (RF_PATH_A, RF_PATH_B):
        st.power_index_offset[p] = 0
        st.delta_power_index[p] = 0
        st.delta_power_index_last[p] = 0
        st.absolute_ofdm_swing_idx[p] = 0
        st.remnant_ofdm_swing_idx[p] = 0
    st.modify_tx_agc_flag_path_a = False
    st.modify_tx_agc_flag_path_b = False
    st.remnant_cck_swing_idx = 0
    st.thermal_value = st.eeprom_thermal


def phy_set_bb_swing_by_band_8812a(st: PhydmState, band_5g: bool, tx_bb_swing_b: int) -> None:
    """The TX-power-tracking reset of phy_SetBBSwingByBand_8812A (rtl8812a_phycfg.c:1153-1167) on a band change.

    ``tx_bb_swing_b`` is the new band's path-B swing, the last phy_get_tx_bb_swing_8812a call.
    """
    if band_5g:
        st.bb_swing_diff_5g = bb_swing_diff(tx_bb_swing_b)
    else:
        st.bb_swing_diff_2g = bb_swing_diff(tx_bb_swing_b)
    bb_diff_between_band = st.bb_swing_diff_2g - st.bb_swing_diff_5g
    if band_5g:
        bb_diff_between_band = -bb_diff_between_band
    st.default_ofdm_index += bb_diff_between_band * 2
    odm_clear_txpowertracking_state(st)


def rtl8812_sreset_status_check(t) -> None:
    """rtl8812_sreset_xmit_status_check + rtl8812_sreset_linked_status_check (rtl8812a_sreset.c:21,73).

    Its rtw_hal_sreset_reset is sreset_reset, empty without DBG_CONFIG_ERROR_RESET (autoconf.h:311).
    """
    t.read32(REG_TXDMA_STATUS)
    t.read32(REG_RXDMA_STATUS)


def phydm_dynamic_ant_weighting_8812a(t) -> None:
    """phydm_rtl8812a.c:65. rssi_min 0 (<= rssi_h2l 37) takes AGC weighting, the 5 GHz writes too."""
    set_bb(t, 0x0854, 1 << 3, 0x1)
    set_bb(t, 0x0850, (1 << 21) | (1 << 22), 0x1)
    query_bb(t, 0x0F94, 0x7)


def phydm_nhm_get_result(t) -> None:
    """phydm_ccx.c:312."""
    set_bb(t, 0x0994, 1 << 1, 0)
    if not query_bb(t, 0x0FB4, 1 << 16):     # phydm_nhm_check_rdy
        return
    t.read32(0x0FA8)
    t.read32(0x0FAC)
    t.read32(0x0FB0)
    t.read32(0x0FB4)


def phydm_clm_get_result(t) -> None:
    """phydm_ccx.c:1303."""
    set_bb(t, 0x0994, 1 << 0, 0x0)
    if not query_bb(t, 0x0FA4, 1 << 16):     # phydm_clm_check_rdy
        return
    query_bb(t, 0x0FA4, 0xFFFF)


def phydm_fa_cnt_statistics_ac(t) -> tuple[int, int]:
    """phydm_dig.c:1801. Returns (cnt_all, cnt_cck_fail)."""
    t.read32(0x0F50)
    query_bb(t, 0x0FCC, 0xFFFF)
    query_bb(t, 0x0FC8, 0xFFFF0000)
    t.read32(ODM_REG_OFDM_FA_TYPE1_11AC)
    t.read32(ODM_REG_OFDM_FA_TYPE2_11AC)
    t.read32(ODM_REG_OFDM_FA_TYPE3_11AC)
    t.read32(ODM_REG_OFDM_FA_TYPE4_11AC)
    t.read32(ODM_REG_OFDM_FA_TYPE5_11AC)
    t.read32(ODM_REG_OFDM_FA_TYPE6_11AC)
    cnt_ofdm_fail = query_bb(t, 0x0F48, 0xFFFF)
    cnt_cck_fail = query_bb(t, ODM_REG_CCK_FA_11AC, 0xFFFF)
    t.read32(ODM_REG_CCK_CCA_CNT_11AC)
    t.read32(ODM_REG_CCK_CRC32_CNT_11AC)
    t.read32(ODM_REG_OFDM_CRC32_CNT_11AC)
    t.read32(0x0F1C)
    t.read32(ODM_REG_HT_CRC32_CNT_11AC)
    t.read32(0x0F18)
    t.read32(ODM_REG_VHT_CRC32_CNT_11AC)
    t.read32(0x0F54)
    cck_enable = query_bb(t, ODM_REG_BB_RX_PATH_11AC, 1 << 28)
    if cck_enable:
        return cnt_ofdm_fail + cnt_cck_fail, cnt_cck_fail
    return cnt_ofdm_fail, cnt_cck_fail


def phydm_get_dbg_port_info(t) -> None:
    """phydm_dig.c:1924, with phydm_get_edcca_report's port (adaptivity_dbg_port 0x209, 11AC)."""
    for debug_port in (0x0, 0x209):
        set_bb(t, 0x08FC, 0xFFFFFFFF, debug_port)     # phydm_set_bb_dbg_port (11AC)
        t.read32(0x0FA0)                              # phydm_get_bb_dbg_port_val
        set_bb(t, 0x08F8, 0x03C00000, 0)              # phydm_release_bb_dbg_port: header_sel(0)


def phydm_false_alarm_counter_reg_reset(t) -> None:
    """phydm_dig.c:1567 (11AC)."""
    set_bb(t, 0x09A4, 1 << 17, 1)
    set_bb(t, 0x09A4, 1 << 17, 0)
    set_bb(t, 0x0A2C, 1 << 15, 0)
    set_bb(t, 0x0A2C, 1 << 15, 1)
    set_bb(t, 0x0B58, 1 << 0, 1)       # phydm_reset_bb_hw_cnt (phydm_api.c:69)
    set_bb(t, 0x0B58, 1 << 0, 0)


def phydm_false_alarm_counter_statistics(t, st: PhydmState) -> tuple[int, int]:
    """phydm_dig.c:2072. Returns phydm_fa_cnt_statistics_ac's counts."""
    cnt_all, cnt_cck_fail = phydm_fa_cnt_statistics_ac(t)
    phydm_get_dbg_port_info(t)
    phydm_false_alarm_counter_reg_reset(t)
    # phydm_set_crc32_cnt2_rate (phydm_dig.c:1945), PHYDM_IC_AC: 0xb04.
    if not st.ofdm2_rate_idx:
        set_bb(t, 0x0B04, 0x0000F000, PHYDM_SPEC_RATE_6M)
        st.ofdm2_rate_idx = ODM_RATE6M
    if not st.ht2_rate_idx:
        set_bb(t, 0x0B04, 0x007F0000, ODM_RATEMCS0 - ODM_RATEMCS0)                # rate digit
        st.ht2_rate_idx = ODM_RATEMCS0
    if not st.vht2_rate_idx:
        set_bb(t, 0x0B04, 0x0F000000, (ODM_RATEVHTSS1MCS0 - ODM_RATEVHTSS1MCS0) % 10)
        set_bb(t, 0x0B04, 0x30000000, 1 - 1)                                       # num_ss - 1
        st.vht2_rate_idx = ODM_RATEVHTSS1MCS0
    return cnt_all, cnt_cck_fail


def odm_write_dig(t, st: PhydmState, new_igi: int) -> None:
    """phydm_dig.c:528. EDCCA mode is normal, so the adaptivity re-run is skipped."""
    if st.cur_ig_value != new_igi:
        set_bb(t, ODM_REG_IGI_A_11AC, ODM_BIT_IGI_11AC, new_igi)    # phydm_write_dig_reg_c50
        set_bb(t, ODM_REG_IGI_B_11AC, ODM_BIT_IGI_11AC, new_igi)
        st.cur_ig_value = new_igi


def phydm_dig(t, st: PhydmState, cnt_all: int) -> None:
    """phydm_dig.c:1397, no link (DFS master off, so dfs_mode_en is false)."""
    dm_dig_min = DIG_MIN_COVERAGE                          # phydm_dig_abs_boundary_decision
    rx_gain_range_max = DIG_MAX_OF_MIN_BALANCE_MODE        # phydm_dig_dym_boundary_decision
    rx_gain_range_min = dm_dig_min
    if rx_gain_range_min > rx_gain_range_max:              # phydm_dig_abnormal_case
        rx_gain_range_min = rx_gain_range_max
    fa_th = (2000, 4000, 5000)                             # phydm_fa_threshold_check
    igi = st.cur_ig_value                                  # phydm_get_new_igi, step {2, 1, 2}
    if cnt_all > fa_th[2]:
        igi = igi + 2
    elif cnt_all > fa_th[1]:
        igi = igi + 1
    elif cnt_all < fa_th[0]:
        igi = igi - 2
    if igi < rx_gain_range_min:
        igi = rx_gain_range_min
    if igi >= rx_gain_range_max:
        igi = rx_gain_range_max
    odm_write_dig(t, st, igi)


def phydm_set_cckpd_lv_type1(t, st: PhydmState, lv: int) -> None:
    """phydm_cck_pd.c:47."""
    if st.cck_pd_lv == lv:
        return
    st.cck_pd_lv = lv
    st.cck_fa_ma = CCK_FA_MA_RESET
    if lv == CCK_PD_LV_1:
        pd_th = 0x83
    else:
        pd_th = 0x40
    t.write8(0x0A0A, pd_th)          # phydm_write_cck_pd_type1


def phydm_cck_pd_th(t, st: PhydmState, cck_fa: int) -> None:
    """phydm_cck_pd.c:1629 + phydm_cckpd_type1 (:78), no link."""
    if st.cck_fa_ma == CCK_FA_MA_RESET:
        st.cck_fa_ma = cck_fa
    else:
        st.cck_fa_ma = (st.cck_fa_ma * 3 + cck_fa) >> 2
    if st.cck_fa_ma > 1000:
        phydm_set_cckpd_lv_type1(t, st, CCK_PD_LV_1)
    elif st.cck_fa_ma < 500:
        phydm_set_cckpd_lv_type1(t, st, CCK_PD_LV_0)


def phydm_adaptivity(t, st: PhydmState) -> None:
    """phydm_adaptivity.c:794 -> phydm_edcca_thre_calc (:535), ODM_IC_PWDB_EDCCA in normal mode.

    The mode is normal because rfctl->adaptivity_en is 0 (rtw_odm.c:144).
    """
    set_bb(t, 0x0800, 1 << 10, 1)            # 8812 EDCCA hang fix: ADC_mask disable
    set_bb(t, 0x0800, 1 << 10, 0)            # ADC_mask enable
    th_l2h = 70 - st.cur_ig_value
    th_h2l = th_l2h - EDCCA_HL_DIFF_NORMAL
    if th_h2l < st.h2l_lb:
        th_h2l = st.h2l_lb
    if th_l2h < st.l2h_lb:
        th_l2h = st.l2h_lb
    set_bb(t, 0x08A4, 0x000000FF, th_l2h & 0xFF)     # phydm_set_edcca_threshold (11AC)
    set_bb(t, 0x08A4, 0x0000FF00, th_h2l & 0xFF)


def phydm_rrsr_mask(t, st: PhydmState) -> None:
    """phydm_rainfo.c:1936 (dynamic_rrsr_en from rtw_en_dyn_rrsr = 1), no link."""
    if st.rrsr_val_curr == st.rrsr_val_init:          # phydm_masked_rrsr_set_register
        return
    st.rrsr_val_curr = st.rrsr_val_init
    set_bb(t, 0x0440, 0xFFFFF, st.rrsr_val_init)


def phy_lc_calibrate_8812a(t) -> None:
    """_phy_lc_calibrate_8812a (halrf_8812a_ce.c:1158)."""
    reg0x914 = t.read32(REG_SINGLE_TONE_CONT_TX_JAGUAR)
    lc_cal = query_rf(t, RF_PATH_A, RF_CHNLBW, RFREG_WRITE_MASK)
    if (reg0x914 & 0x70000) == 0:
        t.write8(REG_TXPAUSE, 0xFF)
    tmp = query_rf(t, RF_PATH_A, RF_LCK, RFREG_WRITE_MASK)
    set_rf_reg(t, RF_PATH_A, RF_LCK, RFREG_WRITE_MASK, tmp | (1 << 14))
    lc_cal = query_rf(t, RF_PATH_A, RF_CHNLBW, RFREG_WRITE_MASK)
    set_rf_reg(t, RF_PATH_A, RF_CHNLBW, RFREG_WRITE_MASK, lc_cal | 0x08000)
    time.sleep(0.150)
    for _ in range(5):
        if query_rf(t, RF_PATH_A, RF_CHNLBW, 0x8000) != 0x1:
            break
        time.sleep(0.010)
    set_rf_reg(t, RF_PATH_A, RF_CHNLBW, RFREG_WRITE_MASK, lc_cal)
    tmp = query_rf(t, RF_PATH_A, RF_LCK, RFREG_WRITE_MASK)
    set_rf_reg(t, RF_PATH_A, RF_LCK, RFREG_WRITE_MASK, tmp & ~(1 << 14))
    if (reg0x914 & 70000) == 0:          # decimal 70000 as in the C (halrf_8812a_ce.c:1209)
        t.write8(REG_TXPAUSE, 0x00)
    set_rf_reg(t, RF_PATH_A, RF_CHNLBW, RFREG_WRITE_MASK, lc_cal)


def pwr_tracking_tx_rate(channel: int) -> int:
    """rf->p_rate_index: _rtw_phydm_pwr_tracking_rate_check (hal_dm.c:1716) with mlmeext tx_rate 1M."""
    if channel > 14:
        return MGN_6M
    return MGN_1M


def is_cck_rate(tx_rate: int) -> bool:
    return tx_rate in (MGN_1M, MGN_2M, MGN_5_5M, MGN_11M)


def get_delta_swing_table_8812a(st: PhydmState, channel: int) -> tuple[tuple[int, ...], ...]:
    """halrf_8812a_ce.c:328. Returns (up_a, down_a, up_b, down_b)."""
    tables = st.delta_swing_tables
    if 1 <= channel <= 14:
        if is_cck_rate(pwr_tracking_tx_rate(channel)):
            return (tables.delta_swing_table_idx_2g_cck_a_p, tables.delta_swing_table_idx_2g_cck_a_n,
                    tables.delta_swing_table_idx_2g_cck_b_p, tables.delta_swing_table_idx_2g_cck_b_n)
        return (tables.delta_swing_table_idx_2ga_p, tables.delta_swing_table_idx_2ga_n,
                tables.delta_swing_table_idx_2gb_p, tables.delta_swing_table_idx_2gb_n)
    if 36 <= channel <= 64:
        row = 0
    elif 100 <= channel <= 144:
        row = 1
    elif 149 <= channel <= 177:
        row = 2
    else:
        return (DELTA_SWING_TABLE_IDX_2GA_P_8188E, DELTA_SWING_TABLE_IDX_2GA_N_8188E,
                DELTA_SWING_TABLE_IDX_2GA_P_8188E, DELTA_SWING_TABLE_IDX_2GA_N_8188E)
    return (tables.delta_swing_table_idx_5ga_p[row], tables.delta_swing_table_idx_5ga_n[row],
            tables.delta_swing_table_idx_5gb_p[row], tables.delta_swing_table_idx_5gb_n[row])


def odm_get_tracking_table(st: PhydmState, channel: int, thermal_value: int, delta: int) -> None:
    """halphyrf_ce.c:171."""
    up_a, down_a, up_b, down_b = get_delta_swing_table_8812a(st, channel)
    for p, up, down in ((RF_PATH_A, up_a, down_a), (RF_PATH_B, up_b, down_b)):
        st.delta_power_index_last[p] = st.delta_power_index[p]
        if thermal_value > st.eeprom_thermal:
            st.delta_power_index[p] = up[delta]
        else:
            st.delta_power_index[p] = -down[delta]
        st.absolute_ofdm_swing_idx[p] = st.delta_power_index[p]


def odm_tx_pwr_track_set_pwr8812a(t, st: PhydmState, channel: int, rf_path: int, params: ChipParams) -> None:
    """halrf_8812a_ce.c:87, MIX_MODE (odm_pwrtrk_method, halphyrf_ce.c:371)."""
    tx_rate = pwr_tracking_tx_rate(channel)
    if (MGN_1M <= tx_rate <= MGN_5_5M) or tx_rate == MGN_11M:
        pwr_tracking_limit = 32
    elif MGN_6M <= tx_rate <= MGN_48M:
        pwr_tracking_limit = 30
    elif tx_rate == MGN_54M:
        pwr_tracking_limit = 28
    else:
        pwr_tracking_limit = 24
    # u8 in the C: a negative sum wraps past the limit and takes the over-limit branch.
    final_cck_swing_index = (st.default_cck_index + st.absolute_ofdm_swing_idx[rf_path]) & 0xFF
    final_ofdm_swing_index = (st.default_ofdm_index + st.absolute_ofdm_swing_idx[rf_path]) & 0xFF
    if rf_path == RF_PATH_A:
        reg = REG_A_TX_SCALE_JAGUAR
    else:
        reg = REG_B_TX_SCALE_JAGUAR
    if channel <= 14:
        pp = params.tx_power_2g[rf_path]
    else:
        pp = params.tx_power_5g[rf_path]
    if final_ofdm_swing_index > pwr_tracking_limit:
        if rf_path == RF_PATH_A:
            st.remnant_cck_swing_idx = s8(final_cck_swing_index - pwr_tracking_limit)
        st.remnant_ofdm_swing_idx[rf_path] = s8(final_ofdm_swing_index - pwr_tracking_limit)
        set_bb(t, reg, 0xFFE00000, tx_scaling_table_jaguar[pwr_tracking_limit])
        if rf_path == RF_PATH_A:
            st.modify_tx_agc_flag_path_a = True
        else:
            st.modify_tx_agc_flag_path_b = True
        phy_set_tx_power_level_by_path(t, channel, rf_path, pp, st.remnant_cck_swing_idx,
                                       st.remnant_ofdm_swing_idx[rf_path])
    elif final_ofdm_swing_index <= 0:
        if rf_path == RF_PATH_A:
            st.remnant_cck_swing_idx = s8(final_cck_swing_index)
        st.remnant_ofdm_swing_idx[rf_path] = s8(final_ofdm_swing_index)
        set_bb(t, reg, 0xFFE00000, tx_scaling_table_jaguar[0])
        if rf_path == RF_PATH_A:
            st.modify_tx_agc_flag_path_a = True
        else:
            st.modify_tx_agc_flag_path_b = True
        phy_set_tx_power_level_by_path(t, channel, rf_path, pp, st.remnant_cck_swing_idx,
                                       st.remnant_ofdm_swing_idx[rf_path])
    else:
        set_bb(t, reg, 0xFFE00000, tx_scaling_table_jaguar[final_ofdm_swing_index])
        if rf_path == RF_PATH_A and st.modify_tx_agc_flag_path_a:
            st.remnant_cck_swing_idx = 0
            st.remnant_ofdm_swing_idx[rf_path] = 0
            phy_set_tx_power_level_by_path(t, channel, rf_path, pp, st.remnant_cck_swing_idx,
                                           st.remnant_ofdm_swing_idx[rf_path])
            st.modify_tx_agc_flag_path_a = False
        if rf_path == RF_PATH_B and st.modify_tx_agc_flag_path_b:
            st.remnant_ofdm_swing_idx[rf_path] = 0
            phy_set_tx_power_level_by_path(t, channel, rf_path, pp, st.remnant_cck_swing_idx,
                                           st.remnant_ofdm_swing_idx[rf_path])
            st.modify_tx_agc_flag_path_b = False


def odm_txpowertracking_callback_thermal_meter(t, st: PhydmState, channel: int, params: ChipParams) -> None:
    """halphyrf_ce.c:407. Not linked, so the IQK re-run (:717) never fires."""
    thermal_value = query_rf(t, RF_PATH_A, RF_T_METER_8812A, 0xFC00)   # + thermal offset 0 (no kfree)
    if st.eeprom_thermal == 0xFF:
        return
    st.thermal_value_avg[st.thermal_value_avg_index] = thermal_value
    st.thermal_value_avg_index += 1
    if st.thermal_value_avg_index == AVG_THERMAL_NUM_8812A:
        st.thermal_value_avg_index = 0
    thermal_value_avg = 0
    thermal_value_avg_count = 0
    for value in st.thermal_value_avg:
        if value:
            thermal_value_avg += value
            thermal_value_avg_count += 1
    if thermal_value_avg_count:
        thermal_value = thermal_value_avg // thermal_value_avg_count
        st.thermal_value_delta = thermal_value - st.eeprom_thermal
    delta = abs(thermal_value - st.thermal_value)
    delta_lck = abs(thermal_value - st.thermal_value_lck)
    if delta_lck >= IQK_THRESHOLD:
        st.thermal_value_lck = thermal_value
        phy_lc_calibrate_8812a(t)                         # halrf_lck_trigger (halrf.c:2050)
    if delta > 0:
        delta = abs(thermal_value - st.eeprom_thermal)
        if delta >= TXPWR_TRACK_TABLE_SIZE:
            delta = TXPWR_TRACK_TABLE_SIZE - 1
        odm_get_tracking_table(st, channel, thermal_value, delta)
        for p in (RF_PATH_A, RF_PATH_B):
            if st.delta_power_index[p] == st.delta_power_index_last[p]:
                st.power_index_offset[p] = 0
            else:
                st.power_index_offset[p] = st.delta_power_index[p] - st.delta_power_index_last[p]
    else:
        for p in (RF_PATH_A, RF_PATH_B):
            st.power_index_offset[p] = 0
    if st.power_index_offset[RF_PATH_A] != 0 or st.power_index_offset[RF_PATH_B] != 0:
        for p in (RF_PATH_A, RF_PATH_B):
            odm_tx_pwr_track_set_pwr8812a(t, st, channel, p, params)
    st.thermal_value = thermal_value


def odm_txpowertracking_check_ce(t, st: PhydmState, channel: int, params: ChipParams) -> None:
    """halrf_powertracking_ce.c:819 (halrf_watchdog -> phydm_rf_watchdog)."""
    if not st.tm_trigger:
        set_rf_reg(t, RF_PATH_A, RF_T_METER_8812A, (1 << 17) | (1 << 16), 0x03)
        st.tm_trigger = 1
        return
    odm_txpowertracking_callback_thermal_meter(t, st, channel, params)
    st.tm_trigger = 0


def phydm_nhm_set_th_reg(t, nhm_th: list[int]) -> None:
    """phydm_ccx.c:420."""
    set_bb(t, 0x0998, 0xFFFFFFFF, nhm_th[3] << 24 | nhm_th[2] << 16 | nhm_th[1] << 8 | nhm_th[0])
    set_bb(t, 0x099C, 0xFFFFFFFF, nhm_th[7] << 24 | nhm_th[6] << 16 | nhm_th[5] << 8 | nhm_th[4])
    set_bb(t, 0x09A0, 0x000000FF, nhm_th[8])
    set_bb(t, 0x0994, 0xFFFF0000, nhm_th[10] << 8 | nhm_th[9])


def phydm_nhm_set(t, st: PhydmState) -> None:
    """phydm_ccx.c:585 as phydm_nhm_mntr_chk(262) calls it: NHM_BACKGROUND, NHM_PERIOD_MAX."""
    if (NHM_EXCLUDE_TXON != st.nhm_include_txon or NHM_EXCLUDE_CCA != st.nhm_include_cca
            or NHM_CNT_ALL != st.nhm_divider_opt):
        # BIT_2_BYTE(divi_opt, include_tx, include_cca, 1)
        set_bb(t, 0x0994, 0xF00, NHM_CNT_ALL << 3 | NHM_EXCLUDE_TXON << 2 | NHM_EXCLUDE_CCA << 1 | 1)
        st.nhm_include_txon = NHM_EXCLUDE_TXON
        st.nhm_include_cca = NHM_EXCLUDE_CCA
        st.nhm_divider_opt = NHM_CNT_ALL
    if NHM_PERIOD_MAX != st.nhm_period:
        set_bb(t, 0x0990, 0xFFFF0000, NHM_PERIOD_MAX)
        st.nhm_period = NHM_PERIOD_MAX
    # phydm_nhm_th_update_chk (phydm_ccx.c:471), NHM_BACKGROUND.
    igi_curr = query_bb(t, ODM_REG_IGI_A_11AC, ODM_BIT_IGI_11AC)      # phydm_get_igi
    if igi_curr < 0x10:
        return
    if st.nhm_igi != igi_curr:
        nhm_th = [((igi_curr - CCA_CAP) << 1) & 0xFF]
        for i in range(1, 11):
            nhm_th.append((nhm_th[0] + ((2 * i) << 1)) & 0xFF)
        st.nhm_igi = igi_curr
        phydm_nhm_set_th_reg(t, nhm_th)


def phydm_env_mntr_set_watchdog(t, st: PhydmState) -> None:
    """phydm_ccx.c:3414: phydm_nhm_mntr_chk + trigger, phydm_clm_mntr_chk + trigger."""
    phydm_nhm_set(t, st)
    set_bb(t, 0x0994, 1 << 1, 0)                 # phydm_nhm_trigger
    set_bb(t, 0x0994, 1 << 1, 1)
    if st.clm_period != CLM_PERIOD_MAX:          # phydm_clm_setting
        set_bb(t, 0x0990, 0xFFFF, CLM_PERIOD_MAX)
        st.clm_period = CLM_PERIOD_MAX
    set_bb(t, 0x0994, 1 << 0, 0x0)               # phydm_clm_trigger
    set_bb(t, 0x0994, 1 << 0, 0x1)


def phydm_watchdog(t, st: PhydmState, channel: int, params: ChipParams) -> int:
    """phydm.c:2384. Returns the false-alarm count the DIG step used."""
    phydm_dynamic_ant_weighting_8812a(t)                 # phydm_hw_setting
    phydm_nhm_get_result(t)                              # phydm_env_mntr_result_watchdog
    phydm_clm_get_result(t)
    cnt_all, cnt_cck_fail = phydm_false_alarm_counter_statistics(t, st)
    phydm_dig(t, st, cnt_all)
    phydm_cck_pd_th(t, st, cnt_cck_fail)
    phydm_adaptivity(t, st)
    phydm_rrsr_mask(t, st)                               # phydm_ra_info_watchdog
    odm_txpowertracking_check_ce(t, st, channel, params)  # halrf_watchdog
    phydm_env_mntr_set_watchdog(t, st)
    return cnt_all


def rtw_dynamic_chk_wk_hdl(t, st: PhydmState, channel: int, params: ChipParams) -> int:
    """rtw_cmd.c:2993: sreset checks, then rtl8812_HalDmWatchDog (rtl8812a_dm.c:230)."""
    rtl8812_sreset_status_check(t)
    # rtw_hal_get_hwreg(HW_VAR_FWLPS_RF_ON) (hal_com.c:13750); with check_hw_status 0
    # rtw_hal_check_rxfifo_full makes no access, so bFwPSAwake changes nothing.
    t.read32(REG_RCR)
    return phydm_watchdog(t, st, channel, params)
