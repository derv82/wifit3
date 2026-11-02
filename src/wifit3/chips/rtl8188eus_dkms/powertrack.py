"""RTL8188EUS thermal TX-power tracking — the ``halrf_watchdog`` slot of the DM tick.

SAFETY-CRITICAL: this adjusts the BB TX-power swing by temperature. An incorrect swing
index drives the PA out of spec. Every value here is sourced verbatim and byte-diffed
against the wire; nothing is approximated.

Ports ``odm_txpowertracking_check_ce`` [SRC] halrf_powertracking_ce.c:694 +
``odm_txpowertracking_callback_thermal_meter`` [SRC] halphyrf_ce.c:164 for 8188E / CE /
1T1R / no-link. ``check_ce`` alternates per tick: it ARMS the RF thermal meter
(RF_T_METER 0x42[17:16]=3) on one tick, then on the next READS it back and runs the
callback. The InitHalDm tail already armed it, so the first operational tick runs the
callback.

The callback: read the thermal meter, average it, compute the delta from the efuse
thermal base, look up a swing-index offset, and (if it changed) rewrite the OFDM BB swing
(``set_iqk_matrix_8188e`` -> 0xc80/0xc94/0xc4c) + the CCK TX-FIR (0xa22..0xa29). The
delta-swing table is **rate-selected** [SRC] halrf_8188e_ce.c:538: the no-link TX rate is
``p_rate_index`` = the unlinked low (CCK) rate [SRC] hal_dm.c:1244, so the CCK-A delta
tables apply (``DELTA_TT_2G_CCK_A_*``, the HWImg ``TxPowerTrack_USB`` tables) — not the
OFDM ones. ``phydm_get_thermal_offset`` is 0 (8188E has no kfree thermal-K).

LCK (``_phy_lc_calibrate_8188e``) and IQK (``iqk.do_iqk_8188e``) run at |delta| >= 8, as
the vendor does. A swing past the limit clamps and carries the remnant into the per-rate
TX-AGC (``txpower``). The IQK result feeds the OFDM swing writes (``set_iqk_matrix_8188e``).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import bb, iqk, rf, txpower
from . import powertrack_tbl as T

logger = logging.getLogger(__name__)

# RF thermal meter [SRC] include/Hal8188EPhyReg.h:418 RF_T_METER_88E.
_RF_T_METER = 0x42
_RF_T_METER_MASK = 0xFC00              # RF reg[15:10]
_RF_T_METER_ARM = (1 << 17) | (1 << 16)  # check_ce trigger bits

# BB swing-apply regs [SRC] set_iqk_matrix_8188e (halrf_8188e_ce.c:120-137).
_REG_OFDM_XA_TX_IQ = 0x0C80            # rOFDM0_XATxIQImbalance (OFDM BB swing, MASKDWORD)
_REG_OFDM_XC_TX_AFE = 0x0C94           # rOFDM0_XCTxAFE (MASKH4BITS)
_REG_OFDM_ECCA_TH = 0x0C4C             # rOFDM0_ECCAThreshold (BIT24)
_REG_CCK_FIR = 0x0A22                  # CCK TX-FIR coefficients 0xa22..0xa29

# LCK [SRC] _phy_lc_calibrate_8188e (halrf_8188e_ce.c:1308).
_REG_CONT_TX = 0x0D03                  # continuous/packet-TX mode bits [6:4]
_REG_TXPAUSE = 0x0522
_RF_AC = 0x00
_RF_CHNLBW = 0x18
_MASK12BITS = 0xFFF
_LCK_START = 0x08000                   # RF_CHNLBW bit15
_LCK_SETTLE_S = 0.1                    # ODM_delay_ms(100)

# config [SRC] configure_txpower_track_8188e + halrf_powertracking_ce.h + halrf.h.
_AVG_THERMAL_NUM = 4                   # AVG_THERMAL_NUM_88E
_IQK_THRESHOLD = 8                     # IQK_THRESHOLD (also the LCK threshold)
_TXPWR_TRACK_TABLE_SIZE = 30           # DELTA_SWINGIDX_SIZE
_OFDM_TABLE_SIZE = len(T.OFDM_SWING_TABLE)        # 43
_CCK_TABLE_SIZE = len(T.CCK_SWING_TABLE_CH1_CH13)  # 33
_PWR_LIMIT_OFDM = 30                   # default pwr_tracking_limit_ofdm (+0 dB)
_PWR_LIMIT_CCK = 28                    # pwr_tracking_limit_cck for a CCK tx_rate (-2 dB)
_DEFAULT_OFDM_INDEX = 30               # thermal_meter_init fallback (idx >= table size)
_DEFAULT_CCK_INDEX = 20


@dataclass
class PowerTrackState:
    """Carried thermal-tracking state (``dm_rf_calibration_struct``), kept in sync with
    the chip. ``thermal_value``/``_iqk``/``_lck`` seed to the efuse base; the swing-index
    bases come from the chip's current BB swing."""
    eeprom_thermal: int
    tx_pwr: object                      # efuse.TxPwr2G, for the TX-AGC reset by rate section
    default_ofdm_index: int
    default_cck_index: int
    delta_swing_table_idx_2g_cck_a_p: list[int]    # rf->delta_swing_table_idx_2g_cck_a_p
    delta_swing_table_idx_2g_cck_a_n: list[int]
    channel: int = 1                    # dm->channel, picks the CCK swing table
    tm_trigger: int = 1                 # InitHalDm tail already armed the meter
    thermal_value: int = -1            # -> eeprom_thermal in __post_init__
    thermal_value_iqk: int = -1
    thermal_value_lck: int = -1
    thermal_avg: list = field(default_factory=lambda: [0] * _AVG_THERMAL_NUM)
    thermal_avg_index: int = 0
    delta_power_index: int = 0
    delta_power_index_last: int = 0
    remnant_ofdm_swing_idx: int = 0     # s8, feeds txpower's tpt for OFDM/HT rates
    remnant_cck_swing_idx: int = 0      # s8, feeds txpower's tpt for CCK rates
    modify_tx_agc_flag_path_a: bool = False
    modify_tx_agc_flag_path_a_cck: bool = False
    # IQK [SRC] halrf_powertracking_ce.h:136,225-231.
    iqk_matrix_reg_setting: list = field(default_factory=lambda: [
        iqk.IqkMatrixRegsSetting() for _ in range(iqk.IQK_MATRIX_SETTINGS_NUM)])
    is_iqk_in_progress: bool = False
    is_rf_pi_enable: int = 0
    rege94: int = 0
    rege9c: int = 0
    regeb4: int = 0
    regebc: int = 0
    ADDA_backup: list = field(default_factory=lambda: [0] * iqk.IQK_ADDA_REG_NUM)
    IQK_MAC_backup: list = field(default_factory=lambda: [0] * iqk.IQK_MAC_REG_NUM)
    IQK_BB_backup: list = field(default_factory=lambda: [0] * iqk.IQK_BB_REG_NUM)
    IQK_BB_backup_recover: list = field(default_factory=lambda: [0] * 9)

    def __post_init__(self):
        if self.thermal_value < 0:
            self.thermal_value = self.eeprom_thermal
            self.thermal_value_iqk = self.eeprom_thermal
            self.thermal_value_lck = self.eeprom_thermal


def get_swing_index(c80: int) -> int:
    """``get_swing_index`` [SRC] halrf_powertracking_ce.c:489 — match 0xc80[31:22] against
    ``ofdm_swing_table_new[i] >> 22``."""
    bb_swing = (c80 & 0xFFC00000) >> 22
    for i, tv in enumerate(T.OFDM_SWING_TABLE):
        table_value = (tv >> 22) if tv >= 0x100000 else tv
        if bb_swing == table_value:
            return i
    return _OFDM_TABLE_SIZE


def get_cck_swing_index(a22: int) -> int:
    """``get_cck_swing_index`` [SRC] halrf_powertracking_ce.c:535 — match byte 0xa22 against
    ``cck_swing_table_ch1_ch13_new[i][0]``."""
    for i, row in enumerate(T.CCK_SWING_TABLE_CH1_CH13):
        if a22 == row[0]:
            return i
    return _CCK_TABLE_SIZE


def seed_state(ofdm_swing_raw: int, cck_swing_raw: int, eeprom_thermal: int, tx_pwr,
               txpowertrack_icut: bool) -> PowerTrackState:
    """``odm_txpowertracking_thermal_meter_init`` [SRC] halrf_powertracking_ce.c:566 — the
    default swing indices come from the BB swing the chip held at InitHalDm (0xc80 OFDM /
    0xa22 CCK), seeded with the efuse thermal base. The vendor reads those during InitHalDm and
    *carries* them (``dm.DmSeed``); we do the same instead of re-reading at tick-start.
    ``eeprom_thermal`` is ``efuse[EEPROM_THERMAL_METER_88E=0xBA]``. ``txpowertrack_icut``
    picks the delta tables ``odm_read_and_config_mp_8188e_txpowertrack_usb[_icut]`` loaded."""
    ofdm = get_swing_index(ofdm_swing_raw)
    cck = get_cck_swing_index(cck_swing_raw)
    return PowerTrackState(
        eeprom_thermal=eeprom_thermal,
        tx_pwr=tx_pwr,
        default_ofdm_index=_DEFAULT_OFDM_INDEX if ofdm >= _OFDM_TABLE_SIZE else ofdm,
        default_cck_index=_DEFAULT_CCK_INDEX if cck >= _CCK_TABLE_SIZE else cck,
        delta_swing_table_idx_2g_cck_a_p=(T.DELTA_TT_2G_CCK_A_P_ICUT if txpowertrack_icut
                                          else T.DELTA_TT_2G_CCK_A_P),
        delta_swing_table_idx_2g_cck_a_n=(T.DELTA_TT_2G_CCK_A_N_ICUT if txpowertrack_icut
                                          else T.DELTA_TT_2G_CCK_A_N),
    )


def _set_iqk_matrix(t, ofdm_index: int, iqk_result_x: int, iqk_result_y: int) -> None:
    """``set_iqk_matrix_8188e(RF_PATH_A)`` [SRC] halrf_8188e_ce.c:69 — the OFDM BB swing,
    scaled by the IQK X/Y result when there is one (s32 sign-extended from 10 bits)."""
    ele_D = (T.OFDM_SWING_TABLE[ofdm_index] & 0xFFC00000) >> 22
    if iqk_result_x != 0:
        if iqk_result_x & 0x00000200:
            iqk_result_x |= -0x400                          # | 0xFFFFFC00 as s32
        ele_A = ((iqk_result_x * ele_D) >> 8) & 0x000003FF
        if iqk_result_y & 0x00000200:
            iqk_result_y |= -0x400
        ele_C = ((iqk_result_y * ele_D) >> 8) & 0x000003FF
        value32 = (ele_D << 22) | ((ele_C & 0x3F) << 16) | ele_A
        bb.set_bb_reg(t, _REG_OFDM_XA_TX_IQ, 0xFFFFFFFF, value32)
        bb.set_bb_reg(t, _REG_OFDM_XC_TX_AFE, 0xF0000000, (ele_C & 0x000003C0) >> 6)
        bb.set_bb_reg(t, _REG_OFDM_ECCA_TH, 1 << 24, ((iqk_result_x * ele_D) >> 7) & 0x01)
    else:
        bb.set_bb_reg(t, _REG_OFDM_XA_TX_IQ, 0xFFFFFFFF, T.OFDM_SWING_TABLE[ofdm_index])
        bb.set_bb_reg(t, _REG_OFDM_XC_TX_AFE, 0xF0000000, 0x0)   # MASKH4BITS
        bb.set_bb_reg(t, _REG_OFDM_ECCA_TH, 1 << 24, 0x0)        # BIT24


def _write_cck_fir(t, cck_index: int, channel: int) -> None:
    """Write the 8 CCK TX-FIR coefficients (0xa22..0xa29), ch14 row on channel 14
    [SRC] halrf_8188e_ce.c:454."""
    row = (T.CCK_SWING_TABLE_CH14 if channel == 14 else T.CCK_SWING_TABLE_CH1_CH13)[cck_index]
    for i in range(8):
        t.write8(_REG_CCK_FIR + i, row[i])


def _u8(v: int) -> int:
    return v & 0xFF


def _s8(v: int) -> int:
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def _reset_tx_agc(t, state: PowerTrackState, sections: tuple[int, ...]) -> None:
    for section in sections:
        txpower.set_tx_power_by_rate_section(
            t, state.tx_pwr, state.channel, section,
            state.remnant_cck_swing_idx, state.remnant_ofdm_swing_idx)


def _apply_mix_mode(t, state: PowerTrackState, absolute: int) -> None:
    """``odm_tx_pwr_track_set_pwr88_e(MIX_MODE, RF_PATH_A)`` [SRC] halrf_8188e_ce.c:339.
    The final indices are C ``u8``: a negative sum wraps and takes the over-limit branch."""
    final_cck = _u8(state.default_cck_index + absolute)
    final_ofdm = _u8(state.default_ofdm_index + absolute)
    # channel_mapped_index is 0 [SRC] halphyrf_ce.c:674 (and every 2.4 GHz IQK slot is 0).
    iqk_x, iqk_y = state.iqk_matrix_reg_setting[0].value[0][0:2]
    logger.debug("pwrtrack: apply MIX swing ofdm=%d cck=%d (abs=%d)", final_ofdm, final_cck, absolute)

    if final_ofdm > _PWR_LIMIT_OFDM:
        state.remnant_ofdm_swing_idx = _s8(final_ofdm - _PWR_LIMIT_OFDM)
        _set_iqk_matrix(t, _PWR_LIMIT_OFDM, iqk_x, iqk_y)
        state.modify_tx_agc_flag_path_a = True
        _reset_tx_agc(t, state, (txpower.OFDM, txpower.HT_MCS0_MCS7))
    elif final_ofdm <= 0:
        state.remnant_ofdm_swing_idx = _s8(final_ofdm)
        _set_iqk_matrix(t, 0, iqk_x, iqk_y)
        state.modify_tx_agc_flag_path_a = True
        _reset_tx_agc(t, state, (txpower.OFDM, txpower.HT_MCS0_MCS7))
    else:
        _set_iqk_matrix(t, final_ofdm, iqk_x, iqk_y)
        if state.modify_tx_agc_flag_path_a:
            state.remnant_ofdm_swing_idx = 0
            _reset_tx_agc(t, state, (txpower.OFDM, txpower.HT_MCS0_MCS7))
            state.modify_tx_agc_flag_path_a = False

    if final_cck > _PWR_LIMIT_CCK:
        state.remnant_cck_swing_idx = _s8(final_cck - _PWR_LIMIT_CCK)
        _write_cck_fir(t, _PWR_LIMIT_CCK, state.channel)
        state.modify_tx_agc_flag_path_a_cck = True
        _reset_tx_agc(t, state, (txpower.CCK,))
    elif final_cck <= 0:
        state.remnant_cck_swing_idx = _s8(final_cck)
        _write_cck_fir(t, 0, state.channel)
        state.modify_tx_agc_flag_path_a_cck = True
        _reset_tx_agc(t, state, (txpower.CCK,))
    else:
        _write_cck_fir(t, final_cck, state.channel)
        if state.modify_tx_agc_flag_path_a_cck:
            state.remnant_cck_swing_idx = 0
            _reset_tx_agc(t, state, (txpower.CCK,))
            state.modify_tx_agc_flag_path_a_cck = False


def phy_lc_calibrate(t) -> None:
    """``_phy_lc_calibrate_8188e(dm, is2T=false)`` [SRC] halrf_8188e_ce.c:1308, reached via
    ``halrf_lck_trigger`` -> ``phy_lc_calibrate_8188e``."""
    tmp_reg = t.read8(_REG_CONT_TX)
    continuous_tx = (tmp_reg & 0x70) != 0
    rf_amode = 0
    if continuous_tx:
        t.write8(_REG_CONT_TX, tmp_reg & 0x8F)
        rf_amode = rf.phy_query_rf_reg(t, 0, _RF_AC, _MASK12BITS)
        rf.set_rf_reg(t, 0, _RF_AC, _MASK12BITS, (rf_amode & 0x8FFFF) | 0x10000)
    else:
        t.write8(_REG_TXPAUSE, 0xFF)
    lc_cal = rf.phy_query_rf_reg(t, 0, _RF_CHNLBW, _MASK12BITS)
    rf.set_rf_reg(t, 0, _RF_CHNLBW, _MASK12BITS, lc_cal | _LCK_START)
    time.sleep(_LCK_SETTLE_S)
    if continuous_tx:
        t.write8(_REG_CONT_TX, tmp_reg)
        rf.set_rf_reg(t, 0, _RF_AC, _MASK12BITS, rf_amode)
    else:
        t.write8(_REG_TXPAUSE, 0x00)


def _callback(t, state: PowerTrackState) -> None:
    """``odm_txpowertracking_callback_thermal_meter`` [SRC] halphyrf_ce.c:164 (8188E/CE,
    no-link, delta-small path)."""
    thermal = rf.phy_query_rf_reg(t, 0, _RF_T_METER, _RF_T_METER_MASK)
    # thermal += phydm_get_thermal_offset() == 0 (8188E has no kfree thermal-K).
    thermal = max(0, min(63, thermal))

    state.thermal_avg[state.thermal_avg_index] = thermal
    state.thermal_avg_index += 1
    if state.thermal_avg_index == _AVG_THERMAL_NUM:
        state.thermal_avg_index = 0
    total = sum(v for v in state.thermal_avg if v)
    count = sum(1 for v in state.thermal_avg if v)
    if count:
        thermal = total // count

    delta = abs(thermal - state.thermal_value)
    delta_lck = abs(thermal - state.thermal_value_lck)
    delta_iqk = abs(thermal - state.thermal_value_iqk)
    logger.debug("pwrtrack: thermal=0x%02x base=0x%02x delta=%d (lck=%d iqk=%d)",
                 thermal, state.eeprom_thermal, delta, delta_lck, delta_iqk)

    # halphyrf_ce.c:376-386; is_scan_in_process and rfk_forbidden are never set in monitor mode.
    if delta_lck >= _IQK_THRESHOLD:
        logger.debug("pwrtrack: LCK (delta_lck=%d >= %d)", delta_lck, _IQK_THRESHOLD)
        state.thermal_value_lck = thermal
        phy_lc_calibrate(t)

    if delta > 0:
        ad = abs(thermal - state.eeprom_thermal)
        if ad >= _TXPWR_TRACK_TABLE_SIZE:
            ad = _TXPWR_TRACK_TABLE_SIZE - 1
        state.delta_power_index_last = state.delta_power_index
        # no-link tx_rate = p_rate_index = unlinked low (CCK) rate -> CCK-A delta tables.
        if thermal > state.eeprom_thermal:
            absolute = state.delta_swing_table_idx_2g_cck_a_p[ad]
        else:
            absolute = -1 * state.delta_swing_table_idx_2g_cck_a_n[ad]
        state.delta_power_index = absolute
        offset = (0 if state.delta_power_index == state.delta_power_index_last
                  else state.delta_power_index - state.delta_power_index_last)
        if offset != 0:
            _apply_mix_mode(t, state, absolute)
        state.thermal_value = thermal

    # halphyrf_ce.c:760-772.
    if delta_iqk >= _IQK_THRESHOLD:
        logger.debug("pwrtrack: IQK (delta_iqk=%d >= %d)", delta_iqk, _IQK_THRESHOLD)
        state.thermal_value_iqk = thermal
        if not state.is_iqk_in_progress:
            iqk.do_iqk_8188e(t, state, delta_iqk, thermal, 8)


def thermal_tick(t, state: PowerTrackState) -> None:
    """``odm_txpowertracking_check_ce`` [SRC] halrf_powertracking_ce.c:694 — arm the RF
    thermal meter, or (on the alternate tick) read it back and run the callback."""
    if not state.tm_trigger:
        rf.set_rf_reg(t, 0, _RF_T_METER, _RF_T_METER_ARM, 0x03)
        state.tm_trigger = 1
        return
    state.tm_trigger = 0
    _callback(t, state)
