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

DEFERRED (raise a clear guard, never silently skip): the IQK and LCK re-calibration paths
(fire only at |delta| >= 8 °C; IQK is the same subsystem InitHalDm defers), and the
over/under-swing-limit branches that reset per-rate TX-AGC (need
``phy_set_tx_power_index_by_rate_section``). None are reached at normal temperatures.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import bb, rf
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
    default_ofdm_index: int
    default_cck_index: int
    tm_trigger: int = 1                 # InitHalDm tail already armed the meter
    thermal_value: int = -1            # -> eeprom_thermal in __post_init__
    thermal_value_iqk: int = -1
    thermal_value_lck: int = -1
    thermal_avg: list = field(default_factory=lambda: [0] * _AVG_THERMAL_NUM)
    thermal_avg_index: int = 0
    delta_power_index: int = 0
    delta_power_index_last: int = 0

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


def seed_state(ofdm_swing_raw: int, cck_swing_raw: int, eeprom_thermal: int) -> PowerTrackState:
    """``odm_txpowertracking_thermal_meter_init`` [SRC] halrf_powertracking_ce.c:566 — the
    default swing indices come from the BB swing the chip held at InitHalDm (0xc80 OFDM /
    0xa22 CCK), seeded with the efuse thermal base. The vendor reads those during InitHalDm and
    *carries* them (``dm.DmSeed``); we do the same instead of re-reading at tick-start.
    ``eeprom_thermal`` is ``efuse[EEPROM_THERMAL_METER_88E=0xBA]``."""
    ofdm = get_swing_index(ofdm_swing_raw)
    cck = get_cck_swing_index(cck_swing_raw)
    return PowerTrackState(
        eeprom_thermal=eeprom_thermal,
        default_ofdm_index=_DEFAULT_OFDM_INDEX if ofdm >= _OFDM_TABLE_SIZE else ofdm,
        default_cck_index=_DEFAULT_CCK_INDEX if cck >= _CCK_TABLE_SIZE else cck,
    )


def _set_iqk_matrix(t, ofdm_index: int) -> None:
    """``set_iqk_matrix_8188e`` else-branch (no prior IQK result, RF_PATH_A) [SRC]
    halrf_8188e_ce.c:120 — write the OFDM BB swing + zero the AFE/ECCA IQ fields."""
    bb.set_bb_reg(t, _REG_OFDM_XA_TX_IQ, 0xFFFFFFFF, T.OFDM_SWING_TABLE[ofdm_index])
    bb.set_bb_reg(t, _REG_OFDM_XC_TX_AFE, 0xF0000000, 0x0)   # MASKH4BITS
    bb.set_bb_reg(t, _REG_OFDM_ECCA_TH, 1 << 24, 0x0)        # BIT24


def _write_cck_fir(t, cck_index: int) -> None:
    """Write the 8 CCK TX-FIR coefficients (0xa22..0xa29) for the channel-1..13 swing row
    [SRC] halrf_8188e_ce.c:454 (this card is 2.4 GHz ch 1-13, never ch14)."""
    row = T.CCK_SWING_TABLE_CH1_CH13[cck_index]
    for i in range(8):
        t.write8(_REG_CCK_FIR + i, row[i])


def _apply_mix_mode(t, state: PowerTrackState, absolute: int) -> None:
    """``odm_tx_pwr_track_set_pwr88_e(MIX_MODE, RF_PATH_A)`` [SRC] halrf_8188e_ce.c:339 —
    final index = default + absolute swing offset, clamped to the power-tracking limit,
    applied as BB swing. The over/under-limit branches reset per-rate TX-AGC (deferred)."""
    final_ofdm = state.default_ofdm_index + absolute
    final_cck = state.default_cck_index + absolute

    if final_ofdm > _PWR_LIMIT_OFDM or final_ofdm <= 0:
        # Clamps to the limit and resets per-rate TX-AGC (phy_set_tx_power_index_by_rate
        # _section) — not yet ported; not reached at normal temperatures.
        logger.debug("pwrtrack: OFDM swing %d hit limit (0..%d) DEFERRED -> tick skips",
                     final_ofdm, _PWR_LIMIT_OFDM)
        raise NotImplementedError(
            f"8188e power-track OFDM swing {final_ofdm} hit a limit "
            f"(0..{_PWR_LIMIT_OFDM}); per-rate TX-AGC reset path is deferred")
    logger.debug("pwrtrack: apply MIX swing ofdm=%d cck=%d (abs=%d)",
                 final_ofdm, final_cck, absolute)
    _set_iqk_matrix(t, final_ofdm)
    # if modify_tx_agc_flag_path_a: reset TX-AGC — flag is False until a limit is hit.

    if final_cck > _PWR_LIMIT_CCK or final_cck <= 0:
        logger.debug("pwrtrack: CCK swing %d hit limit (0..%d) DEFERRED -> tick skips",
                     final_cck, _PWR_LIMIT_CCK)
        raise NotImplementedError(
            f"8188e power-track CCK swing {final_cck} hit a limit "
            f"(0..{_PWR_LIMIT_CCK}); per-rate TX-AGC reset path is deferred")
    _write_cck_fir(t, final_cck)
    # if modify_tx_agc_flag_path_a_cck: reset TX-AGC — flag is False until a limit is hit.


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

    if delta_lck >= _IQK_THRESHOLD:
        logger.debug("pwrtrack: LCK VCO re-cal DEFERRED (delta_lck=%d >= %d) -> tick skips",
                     delta_lck, _IQK_THRESHOLD)
        raise NotImplementedError(
            "8188e power-track LCK (VCO re-cal) deferred (delta_LCK >= 8 C)")

    if delta > 0:
        ad = abs(thermal - state.eeprom_thermal)
        if ad >= _TXPWR_TRACK_TABLE_SIZE:
            ad = _TXPWR_TRACK_TABLE_SIZE - 1
        state.delta_power_index_last = state.delta_power_index
        # no-link tx_rate = p_rate_index = unlinked low (CCK) rate -> CCK-A delta tables.
        if thermal > state.eeprom_thermal:
            absolute = T.DELTA_TT_2G_CCK_A_P[ad]
        else:
            absolute = -1 * T.DELTA_TT_2G_CCK_A_N[ad]
        state.delta_power_index = absolute
        offset = (0 if state.delta_power_index == state.delta_power_index_last
                  else state.delta_power_index - state.delta_power_index_last)
        if offset != 0:
            _apply_mix_mode(t, state, absolute)
        state.thermal_value = thermal

    if delta_iqk >= _IQK_THRESHOLD:
        logger.debug("pwrtrack: IQK DEFERRED (delta_iqk=%d >= %d) -> tick skips",
                     delta_iqk, _IQK_THRESHOLD)
        raise NotImplementedError(
            "8188e power-track IQK deferred (delta_IQK >= 8 C; same subsystem InitHalDm "
            "defers)")


def thermal_tick(t, state: PowerTrackState) -> None:
    """``odm_txpowertracking_check_ce`` [SRC] halrf_powertracking_ce.c:694 — arm the RF
    thermal meter, or (on the alternate tick) read it back and run the callback."""
    if not state.tm_trigger:
        rf.set_rf_reg(t, 0, _RF_T_METER, _RF_T_METER_ARM, 0x03)
        state.tm_trigger = 1
        return
    state.tm_trigger = 0
    _callback(t, state)
