"""RTL8821AU (DKMS) 2 s dynamic-check tick: rtw_dynamic_chk_wk_hdl (core/rtw_cmd.c:2992), always-monitor.

In wire order: the sreset status polls, dm_DynamicUsbTxAgg, rtl8812_HalDmWatchDog's FWLPS_RF_ON RCR read,
then phydm_watchdog (phydm.c:2382) members that touch the chip for the 8821a: hw_setting, FA counters,
DIG, CCK-PD, adaptivity, RRSR mask, TX power tracking, NHM/CLM.

phydm sees no link: MI_ASSOC needs a linked STA/AP (rtw_mi.c:404), so rssi_min stays 0. The monitor
iface itself is WIFI_ASOC_STATE (CONFIG_MONITOR_MODE_XMIT, ioctl_cfg80211.c:2779), which
dm_DynamicUsbTxAgg checks directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .dig import _dbg_port_read, _set_edcca_threshold
from .efuse import PathTxPwr
from .rf import RF_PATH_A, query_bb, query_rf_reg, set_bb, set_rf_reg
from .txpower import phy_set_tx_power_level_by_path

WATCHDOG_PERIOD_S = 2.0         # dynamic_chk_timer, os_intfs.c:4044

REG_TXDMA_STATUS = 0x0210
REG_RXDMA_STATUS = 0x0288
REG_RXDMA_AGG_PG_TH = 0x0280
REG_RCR = 0x0608
REG_RRSR = 0x0440
REG_RA_TRY_RATE_AGG_LMT = 0x04CF
REG_PWED_TH_JAGUAR = 0x0830
REG_BW_INDICATION_JAGUAR = 0x0834
REG_IGI_A = 0x0C50
IGI_MASK = 0x7F
REG_CCK_PD = 0x0A0A
REG_A_TX_SCALE_JAGUAR = 0x0C1C
REG_CRC32_CNT2_RATE = 0x0B04
REG_CCX = 0x0994                # NHM / CLM control (ODM_IC_11AC_SERIES)
REG_CCX_PERIOD = 0x0990         # [31:16] NHM period, [15:0] CLM period
REG_NHM_TH0_3 = 0x0998
REG_NHM_TH4_7 = 0x099C
REG_NHM_TH8 = 0x09A0
REG_NHM_RPT = 0x0FB4            # BIT16 ready, [15:0] duration
REG_CLM_RPT = 0x0FA4            # BIT16 ready, [15:0] result
RF_T_METER = 0x42               # RF_T_METER_8812A / RF_T_METER_NEW

# phydm_fa_cnt_statistics_ac read order (phydm_dig.c:1726): FA types 1-6, OFDM FA, CCK FA, CCA, CCK CRC32,
# OFDM CRC32, OFDM2 CRC32, HT CRC32, HT2 CRC32, VHT CRC32, VHT2 CRC32.
FA_CNT_REGS = (0x0FCC, 0x0FD0, 0x0FBC, 0x0FC0, 0x0FC4, 0x0FC8, 0x0F48, 0x0A5C, 0x0F08, 0x0F04, 0x0F14,
               0x0F1C, 0x0F10, 0x0F18, 0x0F0C, 0x0F54)
REG_OFDM_FA = 0x0F48
REG_CCK_FA = 0x0A5C
REG_BB_RX_PATH = 0x0808         # BIT28 = CCK enabled
ADAPTIVITY_DBG_PORT = 0x209     # phydm_adaptivity_init, ODM_IC_11AC_SERIES

DIG_MIN_COVERAGE = 0x1C
DIG_MAX_OF_MIN_BALANCE_MODE = 0x2A
FA_TH_NO_LINK = (2000, 4000, 5000)      # phydm_fa_threshold_check, !linked, !dfs
EDCCA_HL_DIFF_NORMAL = 8
CCK_FA_MA_RESET = 0xFFFFFFFF
CCK_PD_LV_0, CCK_PD_LV_1 = 0, 1
CCK_PD_TH = {CCK_PD_LV_0: 0x40, CCK_PD_LV_1: 0x83}   # phydm_set_cckpd_lv_type1

CCA_CAP = 14
NHM_PERIOD_MAX = 65534
CLM_PERIOD_MAX = 65535
NHM_TH_NUM = 11
NHM_INCLUDE_INIT = -1           # NHM_TXON_INIT / NHM_CCA_INIT / NHM_CNT_INIT: never equal to a set value

# halrf TX power tracking, 8821a USB (configure_txpower_track_8821a, halhwimg8821a_rf.c:1150-1157).
AVG_THERMAL_NUM = 4
TXPWR_TRACK_TABLE_SIZE = 30
OFDM_TABLE_SIZE = 43
TX_SCALING_TABLE_JAGUAR = (
    0x081, 0x088, 0x090, 0x099, 0x0A2, 0x0AC, 0x0B6, 0x0C0, 0x0CC, 0x0D8, 0x0E5, 0x0F2, 0x101, 0x110,
    0x120, 0x131, 0x143, 0x156, 0x16A, 0x180, 0x197, 0x1AF, 0x1C8, 0x1E3, 0x200, 0x21E, 0x23E, 0x261,
    0x285, 0x2AB, 0x2D3, 0x2FE, 0x32B, 0x35C, 0x38E, 0x3C4, 0x3FE)
DELTA_SWING_2GA_P = (0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 12,
                     12, 12, 12)
DELTA_SWING_2GA_N = (0, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4, 5, 5, 5, 6, 6, 6, 7, 7, 7, 8, 8, 8, 9, 9, 9, 10,
                     10, 10)
DELTA_SWING_5GA = (0, 0, 1, 2, 3, 3, 4, 5, 6, 6, 7, 8, 9, 9, 10, 11, 12, 12, 13, 14, 15, 15, 16, 16, 16, 16,
                   16, 16, 16, 16)      # all three 5 GHz sub-bands, up and down
MGN_1M, MGN_6M = 0x02, 0x0C     # _rtw_phydm_pwr_tracking_rate_check: 1M on 2.4 GHz, 6M on 5 GHz
BB_SWING_DIFF = {0x200: 0, 0x16A: -3, 0x101: -6, 0x0B6: -9}   # phy_get_tx_bb_swing_8812a

# dm_DynamicUsbTxAgg (hal_com.c:14919), 8821U, RX_AGG_USB.
USB_AGG_TX_TP_MBPS = 2
USB_AGG_LAST_TX_BYTES = 220000


def _s8(value: int) -> int:
    v = value & 0xFF
    if v & 0x80:
        return v - 0x100
    return v


def swing_index(bb_swing: int) -> int:
    """[SRC] get_swing_index (halrf_powertracking_ce.c:595): TX_SCALING_TABLE_JAGUAR index, or its size."""
    for i, value in enumerate(TX_SCALING_TABLE_JAGUAR):
        if value == bb_swing:
            return i
    return len(TX_SCALING_TABLE_JAGUAR)


def default_ofdm_index(bb_swing_2g: int) -> int:
    """[SRC] odm_txpowertracking_thermal_meter_init (:684), ODM_RTL8821 branch."""
    index = swing_index(bb_swing_2g)
    return 30 if index >= OFDM_TABLE_SIZE else index


@dataclass
class DmState:
    """phydm / halrf state the tick carries across fires."""
    cur_ig_value: int               # phydm_dig_init: IGI read at InitHalDm
    nhm_igi: int                    # phydm_nhm_init: IGI read at InitHalDm
    rrsr_val_init: int              # phydm_ra_info_init 0x440 read, then rtw_phydm_set_rrsr
    h2l_lb: int                     # phydm_search_pwdb_lower_bound
    l2h_lb: int
    eeprom_thermal: int
    default_ofdm_index: int
    bb_swing_diff_2g: int
    bb_swing_diff_5g: int
    rrsr_val_curr: int = 0
    tx_bytes: int = 0               # xmitpriv.tx_bytes: every injected frame's length
    last_tx_bytes: int = 0
    crc32_cnt2_rate_set: bool = False
    cck_fa_ma: int = CCK_FA_MA_RESET
    cck_pd_lv: int = CCK_PD_LV_1    # phydm_cck_pd_init sets LV_1
    nhm_include_txon: int = NHM_INCLUDE_INIT
    nhm_include_cca: int = NHM_INCLUDE_INIT
    nhm_divider_opt: int = NHM_INCLUDE_INIT
    nhm_period: int = 0
    nhm_ongoing: bool = False
    clm_period: int = CLM_PERIOD_MAX
    clm_ongoing: bool = False
    tm_trigger: bool = False
    thermal_value: int = 0
    thermal_value_avg: list[int] = field(default_factory=lambda: [0] * AVG_THERMAL_NUM)
    thermal_value_avg_index: int = 0
    delta_power_index: int = 0
    delta_power_index_last: int = 0
    absolute_ofdm_swing_idx: int = 0
    remnant_ofdm_swing_idx: int = 0
    remnant_cck_swing_idx: int = 0
    default_cck_index: int = 0      # get_cck_swing_index finds no 8821 branch (halrf_powertracking_ce.c:640)
    modify_tx_agc_flag_path_a: bool = False

    def __post_init__(self) -> None:
        self.thermal_value = self.eeprom_thermal


def clear_txpowertracking_state(st: DmState) -> None:
    """[SRC] odm_clear_txpowertracking_state (halphyrf_ce.c:134), path A; every phy_SwChnlAndSetBwMode8812."""
    st.delta_power_index = 0
    st.delta_power_index_last = 0
    st.absolute_ofdm_swing_idx = 0
    st.remnant_ofdm_swing_idx = 0
    st.modify_tx_agc_flag_path_a = False
    st.remnant_cck_swing_idx = 0
    st.thermal_value = st.eeprom_thermal


def set_bb_swing_by_band(st: DmState, band_5g: bool) -> None:
    """[SRC] phy_SetBBSwingByBand_8812A (rtl8812a_phycfg.c:1130) band-change index shift; the caller clears."""
    diff = st.bb_swing_diff_2g - st.bb_swing_diff_5g
    st.default_ofdm_index += -diff * 2 if band_5g else diff * 2


def _dynamic_usb_tx_agg(t, st: DmState) -> None:
    """[SRC] collect_traffic_statistics (rtw_mlme.c:4041) + dm_DynamicUsbTxAgg; monitor RX is not counted."""
    cur_tx_bytes = st.tx_bytes - st.last_tx_bytes
    st.last_tx_bytes = st.tx_bytes
    cur_tx_tp = cur_tx_bytes * 8 // 2 // 1024 // 1024
    if cur_tx_tp > USB_AGG_TX_TP_MBPS:
        t.write16(REG_RXDMA_AGG_PG_TH, 0x1010)
    elif st.last_tx_bytes > USB_AGG_LAST_TX_BYTES:
        t.write16(REG_RXDMA_AGG_PG_TH, 0x1006)
    else:
        t.write16(REG_RXDMA_AGG_PG_TH, 0x2005)


def _hw_setting(t) -> None:
    """[SRC] odm_hw_setting_8821a (phydm_rtl8821a.c:96), rssi_min 0."""
    t.write8(REG_RA_TRY_RATE_AGG_LMT, 0x00)
    set_bb(t, REG_PWED_TH_JAGUAR, 0x1FF0, 0x11A)
    set_bb(t, REG_BW_INDICATION_JAGUAR, 1 << 26, 1)


def _false_alarm_counter_statistics(t) -> tuple[int, int]:
    """[SRC] phydm_false_alarm_counter_statistics (phydm_dig.c:1940): (cnt_all, cnt_cck_fail)."""
    counts = {reg: t.read32(reg) for reg in FA_CNT_REGS}
    cck_enable = query_bb(t, REG_BB_RX_PATH, 1 << 28)
    ofdm_fail = counts[REG_OFDM_FA] & 0xFFFF
    cck_fail = counts[REG_CCK_FA] & 0xFFFF
    cnt_all = ofdm_fail + cck_fail if cck_enable else ofdm_fail
    _dbg_port_read(t, 0x0)                      # phydm_get_dbg_port_info
    _dbg_port_read(t, ADAPTIVITY_DBG_PORT)      # phydm_get_edcca_report
    set_bb(t, 0x09A4, 1 << 17, 1)               # phydm_false_alarm_counter_reg_reset (11AC)
    set_bb(t, 0x09A4, 1 << 17, 0)
    set_bb(t, 0x0A2C, 1 << 15, 0)
    set_bb(t, 0x0A2C, 1 << 15, 1)
    set_bb(t, 0x0B58, 1 << 0, 1)                # phydm_reset_bb_hw_cnt
    set_bb(t, 0x0B58, 1 << 0, 0)
    return cnt_all, cck_fail


def _set_crc32_cnt2_rates(t, st: DmState) -> None:
    """[SRC] phydm_set_crc32_cnt2_rate (phydm_dig.c:1876) for 6M, MCS0, VHT1SS-MCS0 while unset."""
    if st.crc32_cnt2_rate_set:
        return
    set_bb(t, REG_CRC32_CNT2_RATE, 0x0000F000, 0xB)    # PHYDM_SPEC_RATE_6M
    set_bb(t, REG_CRC32_CNT2_RATE, 0x007F0000, 0)      # MCS0
    set_bb(t, REG_CRC32_CNT2_RATE, 0x0F000000, 0)      # VHT MCS0
    set_bb(t, REG_CRC32_CNT2_RATE, 0x30000000, 0)      # VHT 1SS
    st.crc32_cnt2_rate_set = True


def _dig(t, st: DmState, fa_cnt: int) -> None:
    """[SRC] phydm_dig (phydm_dig.c:1336), not linked, not DFS: step {+2, +1, -2} in [0x1c, 0x2a]."""
    igi = st.cur_ig_value
    if fa_cnt > FA_TH_NO_LINK[2]:
        igi += 2
    elif fa_cnt > FA_TH_NO_LINK[1]:
        igi += 1
    elif fa_cnt < FA_TH_NO_LINK[0]:
        igi -= 2
    igi = max(DIG_MIN_COVERAGE, min(DIG_MAX_OF_MIN_BALANCE_MODE, igi))
    if igi != st.cur_ig_value:          # odm_write_dig; edcca_mode is NORMAL (CONFIG_RTW_ADAPTIVITY_EN 0)
        set_bb(t, REG_IGI_A, IGI_MASK, igi)
        st.cur_ig_value = igi


def _cck_pd_th(t, st: DmState, cck_fa: int) -> None:
    """[SRC] phydm_cck_pd_th + phydm_cckpd_type1 (phydm_cck_pd.c:1617/78), not linked."""
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
    if lv == st.cck_pd_lv:
        return
    st.cck_pd_lv = lv
    st.cck_fa_ma = CCK_FA_MA_RESET
    t.write8(REG_CCK_PD, CCK_PD_TH[lv])


def _adaptivity(t, st: DmState) -> None:
    """[SRC] phydm_adaptivity -> phydm_edcca_thre_calc (phydm_adaptivity.c:535), PWDB EDCCA, NORMAL mode."""
    th_l2h = 70 - st.cur_ig_value
    th_h2l = th_l2h - EDCCA_HL_DIFF_NORMAL
    th_h2l = max(th_h2l, st.h2l_lb)
    th_l2h = max(th_l2h, st.l2h_lb)
    _set_edcca_threshold(t, th_h2l, th_l2h)


def _rrsr_mask(t, st: DmState) -> None:
    """[SRC] phydm_rrsr_mask -> phydm_masked_rrsr_set_register (phydm_rainfo.c:1934/1922), not linked."""
    if st.rrsr_val_curr == st.rrsr_val_init:
        return
    st.rrsr_val_curr = st.rrsr_val_init
    set_bb(t, REG_RRSR, 0xFFFFF, st.rrsr_val_init)


def _tx_pwr_track_set_pwr(t, st: DmState, channel: int, tx_power_2g: PathTxPwr, tx_power_5g: PathTxPwr) -> None:
    """[SRC] odm_tx_pwr_track_set_pwr8821a MIX_MODE, path A (halrf_8821a_ce.c:46)."""
    pwr_tracking_limit = 32 if channel <= 14 else 30    # MGN_1M (CCK) / MGN_6M (OFDM)
    if channel <= 14:
        pp = tx_power_2g
    else:
        pp = tx_power_5g
    # u8 in the C: a negative sum wraps past the limit and takes the over-limit branch.
    final_cck_swing_index = (st.default_cck_index + st.absolute_ofdm_swing_idx) & 0xFF
    final_ofdm_swing_index = (st.default_ofdm_index + st.absolute_ofdm_swing_idx) & 0xFF
    if final_ofdm_swing_index > pwr_tracking_limit:
        st.remnant_cck_swing_idx = _s8(final_cck_swing_index - pwr_tracking_limit)
        st.remnant_ofdm_swing_idx = _s8(final_ofdm_swing_index - pwr_tracking_limit)
        set_bb(t, REG_A_TX_SCALE_JAGUAR, 0xFFE00000, TX_SCALING_TABLE_JAGUAR[pwr_tracking_limit])
        st.modify_tx_agc_flag_path_a = True
        phy_set_tx_power_level_by_path(t, channel, pp, st.remnant_cck_swing_idx, st.remnant_ofdm_swing_idx)
    elif final_ofdm_swing_index <= 0:
        st.remnant_cck_swing_idx = _s8(final_cck_swing_index)
        st.remnant_ofdm_swing_idx = _s8(final_ofdm_swing_index)
        set_bb(t, REG_A_TX_SCALE_JAGUAR, 0xFFE00000, TX_SCALING_TABLE_JAGUAR[0])
        st.modify_tx_agc_flag_path_a = True
        phy_set_tx_power_level_by_path(t, channel, pp, st.remnant_cck_swing_idx, st.remnant_ofdm_swing_idx)
    else:
        set_bb(t, REG_A_TX_SCALE_JAGUAR, 0xFFE00000, TX_SCALING_TABLE_JAGUAR[final_ofdm_swing_index])
        if st.modify_tx_agc_flag_path_a:
            st.remnant_cck_swing_idx = 0
            st.remnant_ofdm_swing_idx = 0
            phy_set_tx_power_level_by_path(t, channel, pp, st.remnant_cck_swing_idx, st.remnant_ofdm_swing_idx)
            st.modify_tx_agc_flag_path_a = False


def _txpowertracking_callback_thermal_meter(t, st: DmState, channel: int, tx_power_2g: PathTxPwr,
                                            tx_power_5g: PathTxPwr) -> None:
    """[SRC] odm_txpowertracking_callback_thermal_meter (halphyrf_ce.c:407), 8821a, not linked (no IQK, no LCK)."""
    thermal_value = query_rf_reg(t, RF_PATH_A, RF_T_METER, 0xFC00)
    if st.eeprom_thermal == 0xFF:
        return
    st.thermal_value_avg[st.thermal_value_avg_index] = thermal_value
    st.thermal_value_avg_index = (st.thermal_value_avg_index + 1) % AVG_THERMAL_NUM
    samples = [value for value in st.thermal_value_avg if value]
    if samples:
        thermal_value = sum(samples) // len(samples)
    delta = abs(thermal_value - st.thermal_value)
    if delta > 0:
        delta = min(abs(thermal_value - st.eeprom_thermal), TXPWR_TRACK_TABLE_SIZE - 1)
        st.delta_power_index_last = st.delta_power_index
        if thermal_value > st.eeprom_thermal:
            table = DELTA_SWING_2GA_P if channel <= 14 else DELTA_SWING_5GA
            st.delta_power_index = table[delta]
        else:
            table = DELTA_SWING_2GA_N if channel <= 14 else DELTA_SWING_5GA
            st.delta_power_index = -table[delta]
        st.absolute_ofdm_swing_idx = st.delta_power_index
        power_index_offset = st.delta_power_index - st.delta_power_index_last
    else:
        power_index_offset = 0
    if power_index_offset != 0:
        _tx_pwr_track_set_pwr(t, st, channel, tx_power_2g, tx_power_5g)     # odm_pwrtrk_method, MIX_MODE
    st.thermal_value = thermal_value


def _txpowertracking_check(t, st: DmState, channel: int, tx_power_2g: PathTxPwr, tx_power_5g: PathTxPwr) -> None:
    """[SRC] odm_txpowertracking_check_ce (halrf_powertracking_ce.c:818): trigger, then read on the next fire."""
    if not st.tm_trigger:
        set_rf_reg(t, RF_PATH_A, RF_T_METER, (1 << 17) | (1 << 16), 0x03)
        st.tm_trigger = True
        return
    _txpowertracking_callback_thermal_meter(t, st, channel, tx_power_2g, tx_power_5g)
    st.tm_trigger = False


def _nhm_mntr_chk(t, st: DmState) -> None:
    """[SRC] phydm_nhm_mntr_chk (phydm_ccx.c:1505): poll the last result, then re-arm NHM_BACKGROUND."""
    set_bb(t, REG_CCX, 1 << 1, 0)                               # phydm_nhm_get_result
    if query_bb(t, REG_NHM_RPT, 1 << 16):
        t.read32(0x0FA8)
        t.read32(0x0FAC)
        t.read32(0x0FB0)
        t.read32(REG_NHM_RPT)
    st.nhm_ongoing = False                                      # phydm_nhm_racing_release
    if st.nhm_include_txon != 0 or st.nhm_include_cca != 0 or st.nhm_divider_opt != 0:   # phydm_nhm_set
        set_bb(t, REG_CCX, 0xF00, 1)
        st.nhm_include_txon = st.nhm_include_cca = st.nhm_divider_opt = 0
    if st.nhm_period != NHM_PERIOD_MAX:
        set_bb(t, REG_CCX_PERIOD, 0xFFFF0000, NHM_PERIOD_MAX)
        st.nhm_period = NHM_PERIOD_MAX
    igi = query_bb(t, REG_IGI_A, IGI_MASK)                      # phydm_nhm_th_update_chk
    if igi < 0x10 or igi == st.nhm_igi:
        return
    st.nhm_igi = igi
    nhm_th = [(((igi - CCA_CAP) << 1) + ((2 * i) << 1)) & 0xFF for i in range(NHM_TH_NUM)]
    t.write32(REG_NHM_TH0_3, nhm_th[0] | nhm_th[1] << 8 | nhm_th[2] << 16 | nhm_th[3] << 24)
    t.write32(REG_NHM_TH4_7, nhm_th[4] | nhm_th[5] << 8 | nhm_th[6] << 16 | nhm_th[7] << 24)
    set_bb(t, REG_NHM_TH8, 0xFF, nhm_th[8])
    set_bb(t, REG_CCX, 0xFFFF0000, nhm_th[9] | nhm_th[10] << 8)


def _clm_mntr_chk(t, st: DmState) -> None:
    """[SRC] phydm_clm_mntr_chk (phydm_ccx.c:2065), CLM_DRIVER_MNTR."""
    set_bb(t, REG_CCX, 1 << 0, 0)                               # phydm_clm_get_result
    if query_bb(t, REG_CLM_RPT, 1 << 16):
        t.read32(REG_CLM_RPT)
    st.clm_ongoing = False                                      # phydm_clm_racing_release
    if st.clm_period != CLM_PERIOD_MAX:                         # phydm_clm_setting
        set_bb(t, REG_CCX_PERIOD, 0xFFFF, CLM_PERIOD_MAX)
        st.clm_period = CLM_PERIOD_MAX


def _env_mntr(t, st: DmState) -> None:
    """[SRC] phydm_env_mntr_watchdog (phydm_ccx.c:2338): NHM + CLM check, then both triggers."""
    _nhm_mntr_chk(t, st)
    _clm_mntr_chk(t, st)
    set_bb(t, REG_CCX, 1 << 1, 0)                               # phydm_nhm_trigger
    set_bb(t, REG_CCX, 1 << 1, 1)
    st.nhm_ongoing = True
    set_bb(t, REG_CCX, 1 << 0, 0)                               # phydm_clm_trigger
    set_bb(t, REG_CCX, 1 << 0, 1)
    st.clm_ongoing = True


def tick(t, st: DmState, channel: int, tx_power_2g: PathTxPwr, tx_power_5g: PathTxPwr) -> None:
    """One rtw_dynamic_chk_wk_hdl fire."""
    t.read32(REG_TXDMA_STATUS)                  # rtl8812_sreset_xmit_status_check
    t.read32(REG_RXDMA_STATUS)                  # rtl8812_sreset_linked_status_check
    _dynamic_usb_tx_agg(t, st)
    t.read32(REG_RCR)                           # HW_VAR_FWLPS_RF_ON (hal_com.c:13605)
    _hw_setting(t)
    cnt_all, cck_fail = _false_alarm_counter_statistics(t)
    _set_crc32_cnt2_rates(t, st)
    _dig(t, st, cnt_all)
    _cck_pd_th(t, st, cck_fail)
    _adaptivity(t, st)
    _rrsr_mask(t, st)
    _txpowertracking_check(t, st, channel, tx_power_2g, tx_power_5g)
    _env_mntr(t, st)
