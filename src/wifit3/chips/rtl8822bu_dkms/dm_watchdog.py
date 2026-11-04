"""RTL8822BU dynamic-check tick: rtw_dynamic_chk_wk_hdl (rtw_cmd.c:2992) for one USB monitor interface
with no link. The members silent in that state are listed in RTL8822BU_DKMS.md."""
from __future__ import annotations

from . import cal, sipi, txpower
from .cal import (AVG_THERMAL_NUM_8822B, CCK_FA_MA_RESET, CCK_PD_LV_0, CCK_PD_LV_1, MASKDWORD,
                  TX_SCALING_TABLE_JAGUAR, DmState)
from .constants import (BIT_EN_PRE_CALC, BIT_RXDMA_AGG_EN, BIT_SHIFT_DMA_AGG_TO, REG_RCR,
                        REG_RXDMA_AGG_PG_TH, REG_TXDMA_PQ_MAP, REG_TXDMA_STATUS)

REG_RXDMA_STATUS_8822B = 0x0288
REG_RXFF_PTR_V1_8822B = 0x1118
# rtl8822bu_ops.c:48-51: monitor frames are not counted in traffic_stat, so cur_tx_tp/cur_rx_tp stay 0.
RXAGG_USB_TIMEOUT_LOW_TRAFFIC, RXAGG_USB_SIZE_LOW_TRAFFIC = 0x01, 0x01

DIG_MAX_OF_MIN_BALANCE_MODE = 0x2A        # [SRC] phydm_dig.h:49, dig_init's dig_max_of_min
DIG_MIN_COVERAGE = 0x1C                   # [SRC] phydm_dig.h:40
_CCK_PD_TH_TYPE1 = {CCK_PD_LV_0: 0x40, CCK_PD_LV_1: 0x83}   # phydm_set_cckpd_lv_type1 (no-link levels)
TH_L2H_DIFF_IGI, EDCCA_TH_L2H_LB, EDCCA_HL_DIFF_NORMAL = 8, 48, 8   # [SRC] phydm_adaptivity.h:33-35
ADAPTIVITY_DBG_PORT_11AC = 0x209          # [SRC] phydm_adaptivity.c:746
PHYDM_SPEC_RATE_6M = 0xB                  # [SRC] phydm_pre_define.h:316
ODM_RATE6M, ODM_RATEMCS0, ODM_RATEVHTSS1MCS0 = 0x04, 0x0C, 0x2C
NHM_PERIOD_MAX, CLM_PERIOD_MAX, FAHM_PERIOD_MAX = 65534, 65535, 65534   # [SRC] phydm_ccx.h:44-60
FAHM_INCLU_FA, FAHM_INCLU_CRC_ERR = 1 << 0, 1 << 2
TXPWR_TRACK_TABLE_SIZE = 30               # [SRC] halrf_powertracking_ce.h:40
RF_T_METER_8822B = 0x42                   # [SRC] halrf_8822b.h:30


def rtw_dynamic_chk_wk_hdl(t, st: DmState, channel: int, txpwr_pg: txpower.TxpwrPG) -> None:
    # xmit_status_check / linked_status_check (rtl8822b_ops.c:844/927), DBG_CONFIG_ERROR_DETECT.
    # TODO: verify, untested here: a non-zero DMA status runs rtw_hal_sreset_reset, which is not ported.
    t.read32(REG_TXDMA_STATUS)
    t.read32(REG_RXDMA_STATUS_8822B)
    t.read16(REG_RXFF_PTR_V1_8822B)                  # check_rx_count
    # dm_DynamicUsbTxAgg (hal_com.c:15035) -> HW_VAR_RXDMA_AGG_PG_TH (rtl8822bu_ops.c:46)
    # -> rtw_halmac_rx_agg_switch(USB, drv_define) -> cfg_usb_rx_agg_88xx (halmac_usb_88xx.c:88)
    dma_usb_agg = t.read8(REG_RXDMA_AGG_PG_TH + 3)
    agg_enable = t.read8(REG_TXDMA_PQ_MAP) | BIT_RXDMA_AGG_EN
    dma_usb_agg &= ~(1 << 7)
    t.write32(REG_RXDMA_AGG_PG_TH, t.read32(REG_RXDMA_AGG_PG_TH) | BIT_EN_PRE_CALC)
    t.write8(REG_TXDMA_PQ_MAP, agg_enable)
    t.write8(REG_RXDMA_AGG_PG_TH + 3, dma_usb_agg)
    t.write16(REG_RXDMA_AGG_PG_TH,
              RXAGG_USB_SIZE_LOW_TRAFFIC | (RXAGG_USB_TIMEOUT_LOW_TRAFFIC << BIT_SHIFT_DMA_AGG_TO))
    # rtl8822b_phy_haldm_watchdog (rtl8822b_phy.c:485): HW_VAR_FWLPS_RF_ON reads RCR (rtl8822b_ops.c:2741);
    # it only gates check_rxfifo_full, which is software with check_hw_status 0.
    t.read32(REG_RCR)
    phydm_watchdog(t, st, channel, txpwr_pg)


def phydm_watchdog(t, st: DmState, channel: int, txpwr_pg: txpower.TxpwrPG) -> None:
    """[SRC] phydm_watchdog (phydm.c:2384), is_linked false, rssi_min 0, no scan."""
    # phydm_phy_info_update -> phydm_get_condi_num_8822b (phydm_hal_api8822b.c:933)
    sipi.set_bb_reg(t, 0x1988, 1 << 22, 0x1)
    sipi.get_bb_reg(t, 0x0F84, (1 << 17) | (1 << 16) | 0xFFFF)
    # phydm_hwsetting_8822b (phydm_rtl8822b.c:539): htstf off and AGC ant weighting for rssi_min 0
    sipi.set_bb_reg(t, 0x08D8, 1 << 17, 0x0)
    sipi.set_bb_reg(t, 0x098C, MASKDWORD, 0x43440000)
    sipi.get_bb_reg(t, 0x0F94, 0x7)
    env_mntr_result_watchdog(t)
    fa = false_alarm_counter_statistics(t, st)
    phydm_dig(t, st, fa["cnt_all"])
    cck_pd_th(t, st, fa["cnt_cck_fail"])
    # phydm_adaptivity -> phydm_edcca_thre_calc (phydm_adaptivity.c:535), NORMAL mode, < JGR2
    th_l2h = max(st.cur_ig_value + TH_L2H_DIFF_IGI, EDCCA_TH_L2H_LB)
    sipi.set_bb_reg(t, 0x08A4, 0x000000FF, th_l2h)
    sipi.set_bb_reg(t, 0x08A4, 0x0000FF00, (th_l2h - EDCCA_HL_DIFF_NORMAL) & 0xFF)
    # phydm_ra_info_watchdog -> phydm_rrsr_mask (phydm_rainfo.c:1940), dynamic_rrsr_en (rtw_en_dyn_rrsr 1)
    if st.rrsr_val_curr != st.rrsr_val_init:
        st.rrsr_val_curr = st.rrsr_val_init
        sipi.set_bb_reg(t, 0x0440, 0xFFFFF, st.rrsr_val_init)
    odm_txpowertracking_check_ce(t, st, channel, txpwr_pg)
    # phydm_dyn_bw_indication -> phydm_bw_fixed_setting (phydm_api.c:800), 20 MHz
    sipi.set_bb_reg(t, 0x0840, 0xF, 0x0)
    sipi.set_bb_reg(t, 0x0840, 1 << 4, 0x1)
    env_mntr_set_watchdog(t, st)


def env_mntr_result_watchdog(t) -> None:
    """[SRC] phydm_env_mntr_result_watchdog (phydm_ccx.c:3377): NHM, CLM, then FAHM report reads."""
    sipi.set_bb_reg(t, 0x0994, 1 << 1, 0)           # phydm_nhm_get_result
    if sipi.get_bb_reg(t, 0x0FB4, 1 << 16):         # phydm_nhm_check_rdy
        for reg in (0x0FA8, 0x0FAC, 0x0FB0, 0x0FB4):
            t.read32(reg)
    sipi.set_bb_reg(t, 0x0994, 1 << 0, 0)           # phydm_clm_get_result
    if sipi.get_bb_reg(t, 0x0FA4, 1 << 16):         # phydm_clm_check_rdy
        sipi.get_bb_reg(t, 0x0FA4, 0xFFFF)
    if sipi.get_bb_reg(t, 0x1F98, 1 << 31):         # phydm_fahm_get_result: phydm_fahm_check_rdy
        for i in range(6):
            t.read32(0x1F80 + (i << 2))
        sipi.get_bb_reg(t, 0x1F98, 0xFFFF)


def false_alarm_counter_statistics(t, st: DmState) -> dict[str, int]:
    """[SRC] phydm_false_alarm_counter_statistics (phydm_dig.c:2072) for 11AC."""
    # phydm_fa_cnt_statistics_ac (phydm_dig.c:1801)
    t.read32(0x0F50)
    sipi.get_bb_reg(t, 0x0FCC, 0xFFFF)
    sipi.get_bb_reg(t, 0x0FC8, 0xFFFF0000)
    for reg in (0x0FCC, 0x0FD0, 0x0FBC, 0x0FC0, 0x0FC4, 0x0FC8):   # ODM_REG_OFDM_FA_TYPE1..6_11AC
        t.read32(reg)
    cnt_ofdm_fail = sipi.get_bb_reg(t, 0x0F48, 0xFFFF)
    cnt_cck_fail = sipi.get_bb_reg(t, 0x0A5C, 0xFFFF)              # ODM_REG_CCK_FA_11AC
    t.read32(0x0F08)                                                # ODM_REG_CCK_CCA_CNT_11AC
    for reg in (0x0F04, 0x0F14, 0x0F1C, 0x0F10, 0x0F18, 0x0F0C, 0x0F54):   # CRC32 counters
        t.read32(reg)
    if sipi.get_bb_reg(t, 0x0808, 1 << 28):                         # cck_enable
        cnt_all = cnt_ofdm_fail + cnt_cck_fail
    else:
        cnt_all = cnt_ofdm_fail
    # phydm_get_dbg_port_info (phydm_dig.c:1934): dbg port 0x0, then phydm_get_edcca_report
    for dbg_port in (0x0, ADAPTIVITY_DBG_PORT_11AC):
        if cal._set_bb_dbg_port(t, st, cal.DBGPORT_PRI_1, dbg_port):
            cal._get_bb_dbg_port_val(t)
            cal._release_bb_dbg_port(t, st)
    # phydm_false_alarm_counter_reg_reset (phydm_dig.c:1567), 11AC
    sipi.set_bb_reg(t, 0x09A4, 1 << 17, 1)
    sipi.set_bb_reg(t, 0x09A4, 1 << 17, 0)
    sipi.set_bb_reg(t, 0x0A2C, 1 << 15, 0)
    sipi.set_bb_reg(t, 0x0A2C, 1 << 15, 1)
    sipi.set_bb_reg(t, 0x0B58, 1 << 0, 1)          # phydm_reset_bb_hw_cnt
    sipi.set_bb_reg(t, 0x0B58, 1 << 0, 0)
    # phydm_set_crc32_cnt2_rate (phydm_dig.c:1957) while each *_rate_idx is still 0
    if not st.ofdm2_rate_idx:
        sipi.set_bb_reg(t, 0x0B04, 0x0000F000, PHYDM_SPEC_RATE_6M)
        st.ofdm2_rate_idx = ODM_RATE6M
    if not st.ht2_rate_idx:
        sipi.set_bb_reg(t, 0x0B04, 0x007F0000, 0)  # MCS0
        st.ht2_rate_idx = ODM_RATEMCS0
    if not st.vht2_rate_idx:
        sipi.set_bb_reg(t, 0x0B04, 0x0F000000, 0)  # VHT MCS0
        sipi.set_bb_reg(t, 0x0B04, 0x30000000, 0)  # 1SS - 1
        st.vht2_rate_idx = ODM_RATEVHTSS1MCS0
    return {"cnt_all": cnt_all, "cnt_cck_fail": cnt_cck_fail}


def phydm_dig(t, st: DmState, fa_metrics: int) -> None:
    """[SRC] phydm_dig (phydm_dig.c:1397), no link, no DFS master: the IGI hunts within
    [DIG_MIN_COVERAGE, dig_max_of_min] by the FA count (phydm_get_new_igi, before-link branch)."""
    rx_gain_range_max = DIG_MAX_OF_MIN_BALANCE_MODE  # phydm_dig_dym_boundary_decision, !is_linked
    rx_gain_range_min = DIG_MIN_COVERAGE
    if rx_gain_range_min > rx_gain_range_max:        # phydm_dig_abnormal_case
        rx_gain_range_min = rx_gain_range_max
    fa_th = (2000, 4000, 5000)                       # phydm_fa_threshold_check, no link, non-DFS
    igi = st.cur_ig_value
    if fa_metrics > fa_th[2]:                        # phydm_new_igi_by_fa, step {+2, +1, -2}
        igi += 2
    elif fa_metrics > fa_th[1]:
        igi += 1
    elif fa_metrics < fa_th[0]:
        igi -= 2
    if igi < rx_gain_range_min:
        igi = rx_gain_range_min
    if igi >= rx_gain_range_max:
        igi = rx_gain_range_max
    cal._odm_write_dig(t, st, igi)


def cck_pd_th(t, st: DmState, cck_fa: int) -> None:
    """[SRC] phydm_cck_pd_th + phydm_cckpd_type1 (phydm_cck_pd.c:1629/78), no link."""
    if st.cck_fa_ma == CCK_FA_MA_RESET:
        st.cck_fa_ma = cck_fa
    else:
        st.cck_fa_ma = (st.cck_fa_ma * 3 + cck_fa) >> 2
    if st.cck_fa_ma > 1000:
        lv = CCK_PD_LV_1
    elif st.cck_fa_ma < 500:
        lv = CCK_PD_LV_0
    else:
        return
    if st.cck_pd_lv == lv:                           # phydm_set_cckpd_lv_type1
        return
    st.cck_pd_lv = lv
    st.cck_fa_ma = CCK_FA_MA_RESET
    t.write8(0x0A0A, _CCK_PD_TH_TYPE1[lv])


def odm_txpowertracking_check_ce(t, st: DmState, channel: int, txpwr_pg: txpower.TxpwrPG) -> None:
    """[SRC] halrf_watchdog -> odm_txpowertracking_check_ce (halrf_powertracking_ce.c:819): alternate
    ticks trigger the thermal meter and run the callback."""
    if not st.tm_trigger:
        sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_T_METER_8822B, (1 << 17) | (1 << 16), 0x03)
        st.tm_trigger = True
        return
    odm_txpowertracking_callback_thermal_meter(t, st, channel, txpwr_pg)
    st.tm_trigger = False


def odm_txpowertracking_callback_thermal_meter(t, st: DmState, channel: int,
                                               txpwr_pg: txpower.TxpwrPG) -> None:
    """[SRC] odm_txpowertracking_callback_thermal_meter (halphyrf_ce.c:407). LCK skips 8822B and
    the IQK branch needs is_linked, so neither is here."""
    thermal_value = sipi.read_rf_reg(t, sipi.RF_PATH_A, RF_T_METER_8822B, 0xFC00)
    thermal_value = min(max(thermal_value + st.thermal_offset, 0), 63)
    if st.eeprom_thermal == 0xFF:
        return
    st.thermal_value_avg[st.thermal_value_avg_index] = thermal_value
    st.thermal_value_avg_index += 1
    if st.thermal_value_avg_index == AVG_THERMAL_NUM_8822B:
        st.thermal_value_avg_index = 0
    nonzero = [v for v in st.thermal_value_avg if v]
    if nonzero:
        thermal_value = sum(nonzero) // len(nonzero)
    if thermal_value != st.thermal_value:
        delta = min(abs(thermal_value - st.eeprom_thermal), TXPWR_TRACK_TABLE_SIZE - 1)
        odm_get_tracking_table(st, channel, thermal_value, delta)
        for p in range(2):
            if st.delta_power_index[p] == st.delta_power_index_last[p]:
                st.power_index_offset[p] = 0
            else:
                st.power_index_offset[p] = st.delta_power_index[p] - st.delta_power_index_last[p]
    else:
        st.power_index_offset = [0, 0]
    if st.power_index_offset[0] != 0 or st.power_index_offset[1] != 0:
        for p in range(2):                           # odm_pwrtrk_method: MIX_MODE per path
            odm_tx_pwr_track_set_pwr8822b(t, st, p, channel, txpwr_pg)
    st.thermal_value = thermal_value


def odm_get_tracking_table(st: DmState, channel: int, thermal_value: int, delta: int) -> None:
    """[SRC] odm_get_tracking_table (halphyrf_ce.c:169) with get_delta_swing_table_8822b
    (halrf_8822b.c:377): the up table above the EFUSE thermal, the negated down table otherwise."""
    tbl = st.delta_swing
    up = [tbl.delta_swing_table_idx_2ga_p, tbl.delta_swing_table_idx_2gb_p]
    down = [tbl.delta_swing_table_idx_2ga_n, tbl.delta_swing_table_idx_2gb_n]
    for band, (lo, hi) in enumerate(((36, 64), (100, 144), (149, 177))):
        if lo <= channel <= hi:
            up = [tbl.delta_swing_table_idx_5ga_p[band], tbl.delta_swing_table_idx_5gb_p[band]]
            down = [tbl.delta_swing_table_idx_5ga_n[band], tbl.delta_swing_table_idx_5gb_n[band]]
    for p in range(2):
        st.delta_power_index_last[p] = st.delta_power_index[p]
        if thermal_value > st.eeprom_thermal:
            st.delta_power_index[p] = up[p][delta]
        else:
            st.delta_power_index[p] = -down[p][delta]
        st.absolute_ofdm_swing_idx[p] = st.delta_power_index[p]


def odm_tx_pwr_track_set_pwr8822b(t, st: DmState, path: int, channel: int,
                                  txpwr_pg: txpower.TxpwrPG) -> None:
    """[SRC] odm_tx_pwr_track_set_pwr8822b + odm_pwrtrack_method_set_pwr8822b MIX_MODE
    (halrf_8822b.c:199/131); unlinked tx_rate is rf->p_rate_index (hal_dm.c:1722)."""
    section = "cck" if channel <= 14 else "ofdm"     # MGN_1M on 2.4 GHz, MGN_6M on 5 GHz
    tx_pwr_idx = min(txpower._pg_base(txpwr_pg, path, channel, section), 63)
    tx_pwr_idx_offset = min(63 - tx_pwr_idx, 0xF)
    # get_mix_mode_tx_agc_bb_swing_offset_8822b (halrf_8822b.c:72)
    default = st.default_ofdm_index
    absolute = st.absolute_ofdm_swing_idx[path]
    if 0 <= absolute <= tx_pwr_idx_offset:
        tx_agc_index = absolute
        tx_bb_swing_index = default
    elif absolute > tx_pwr_idx_offset:
        tx_agc_index = tx_pwr_idx_offset
        st.remnant_ofdm_swing_idx[path] = absolute - tx_pwr_idx_offset
        tx_bb_swing_index = min(default + st.remnant_ofdm_swing_idx[path], default + 10)
    else:
        tx_agc_index = 0
        tx_bb_swing_index = default + absolute if default > -absolute else 0
    st.absolute_ofdm_swing_idx[path] = tx_agc_index
    st.bb_swing_idx_ofdm[path] = tx_bb_swing_index
    sipi.set_bb_reg(t, (0x0C94, 0x0E94)[path], 0x3E000000, tx_agc_index)
    sipi.set_bb_reg(t, (0x0C1C, 0x0E1C)[path], 0xFFE00000, TX_SCALING_TABLE_JAGUAR[tx_bb_swing_index])


def env_mntr_set_watchdog(t, st: DmState) -> None:
    """[SRC] phydm_env_mntr_set_watchdog (phydm_ccx.c:3414): NHM, CLM and FAHM background set +
    trigger, monitor time 262 ms."""
    # phydm_nhm_set (phydm_ccx.c:585): exclude txon, exclude cca, NHM_CNT_ALL
    if (st.nhm_include_txon, st.nhm_include_cca, st.nhm_divider_opt) != (0, 0, 0):
        sipi.set_bb_reg(t, 0x0994, 0xF00, 1)         # BIT_2_BYTE(divi_opt, include_tx, include_cca, 1)
        st.nhm_include_txon, st.nhm_include_cca, st.nhm_divider_opt = 0, 0, 0
    if st.nhm_period != NHM_PERIOD_MAX:
        sipi.set_bb_reg(t, 0x0990, 0xFFFF0000, NHM_PERIOD_MAX)
        st.nhm_period = NHM_PERIOD_MAX
    igi = sipi.get_bb_reg(t, 0x0C50, 0x7F)           # phydm_nhm_th_update_chk
    if igi >= 0x10 and st.nhm_igi != igi:
        st.nhm_igi = igi
        cal.nhm_set_th_reg(t, cal.nhm_th_background(igi))
    sipi.set_bb_reg(t, 0x0994, 1 << 1, 0)            # phydm_nhm_trigger
    sipi.set_bb_reg(t, 0x0994, 1 << 1, 1)
    if st.clm_period != CLM_PERIOD_MAX:              # phydm_clm_setting
        sipi.set_bb_reg(t, 0x0990, 0xFFFF, CLM_PERIOD_MAX)
        st.clm_period = CLM_PERIOD_MAX
    sipi.set_bb_reg(t, 0x0994, 1 << 0, 0)            # phydm_clm_trigger
    sipi.set_bb_reg(t, 0x0994, 1 << 0, 1)
    # phydm_fahm_set (phydm_ccx.c:2155): FAHM_INCLU_FA / FAHM_INCLU_CRC_ERR
    if (st.fahm_numer_opt, st.fahm_denom_opt) != (FAHM_INCLU_FA, FAHM_INCLU_CRC_ERR):
        sipi.set_bb_reg(t, 0x0994, 0xE0, FAHM_INCLU_FA)
        sipi.set_bb_reg(t, 0x0994, 0x7000, FAHM_INCLU_CRC_ERR)
        st.fahm_numer_opt, st.fahm_denom_opt = FAHM_INCLU_FA, FAHM_INCLU_CRC_ERR
        st.fahm_inclu_cck = False                    # the options include FAHM_INCLU_FA
        sipi.set_bb_reg(t, 0x0994, 1 << 4, 0)
    if st.fahm_period != FAHM_PERIOD_MAX:
        sipi.set_bb_reg(t, 0x1CF8, 0xFFFF00, FAHM_PERIOD_MAX)
        st.fahm_period = FAHM_PERIOD_MAX
    igi = sipi.get_bb_reg(t, 0x0C50, 0x7F)           # phydm_fahm_th_update_chk
    if igi >= 0x10 and st.fahm_igi != igi:
        st.fahm_igi = igi
        cal.fahm_set_th_reg(t, cal.nhm_th_background(igi))
    sipi.set_bb_reg(t, 0x0994, 1 << 2, 0)            # phydm_fahm_trigger
    sipi.set_bb_reg(t, 0x0994, 1 << 2, 1)
