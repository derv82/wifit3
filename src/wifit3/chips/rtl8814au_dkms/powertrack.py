"""RTL8814A thermal TX-power tracking (M3c halrf) — port of the vendor phydm MIX_MODE path.

``odm_txpowertracking_callback_thermal_meter`` [SRC halphyrf_ce.c:395] reads the RF thermal
meter (RF reg 0x42[15:10]), averages it, and — when it diverges from the EFUSE base — walks the
delta-swing tables to re-derive each RF path's OFDM BB-swing index, then writes the per-path
TXAGC (0xX94[29:25]) and BB-swing (0xX1C[31:21]) registers. ``odm_txpowertracking_check_ce``
[SRC halrf_powertracking_ce.c:816] is the two-phase gate: one tick arms the meter, the next runs
the callback.

Scope — the no-link 8814A MIX_MODE (``pwt_type == 0``) case. ``mp_mode`` is off, the card never
links, and K-free thermal trim is off, so the thermal-value read equals the raw RF field, and the
LCK / xtal / DPK branches and the tx-power-index headroom split are no-ops here (each noted at its
site). The IQK trigger (``do_iqk_8814a``) that tails the callback is a SEPARATE milestone and is
deliberately left unported — reaching it is where the pcap gate stops.

Register defs [SRC halrf_8814a_ce.c:30-39]; the swing math is
``get_mix_mode_tx_agc_bb_swing_offset`` [SRC halrf_8814a_ce.c:1136]; the table walk is
``odm_get_tracking_table`` [SRC halphyrf_ce.c:167]; the table selection is
``get_delta_swing_table_8814a`` / ``..._path_cd`` [SRC halrf_8814a_ce.c:1407 / 1491].
"""
from __future__ import annotations

from . import constants as C
from . import iqk
from .bb import _set_reg_masked as _bb32
from .rf import _rf_read, set_rf_masked
from . import powertrack_tbl as T

# Per-path TXAGC (OFDM index area) and BB-swing registers + masks [SRC halrf_8814a_ce.c:30-39].
_PATHS = ("a", "b", "c", "d")
REG_TX_AGC = {"a": 0x0C94, "b": 0x0E94, "c": 0x1894, "d": 0x1A94}
REG_BBSWING = {"a": 0x0C1C, "b": 0x0E1C, "c": 0x181C, "d": 0x1A1C}
TXAGC_BITMASK = (1 << 29) | (1 << 28) | (1 << 27) | (1 << 26) | (1 << 25)   # 0x3E000000
BBSWING_BITMASK = 0xFFE00000

RF_T_METER = 0x42                # RF_T_METER_88E [SRC Hal8814PhyReg.h:812]; RF_T_METER_NEW too
_THERMAL_MASK = 0xFC00           # RF 0x42[15:10] = thermal meter (odm_get_rf_reg mask)
_THERMAL_TRIGGER = (1 << 17) | (1 << 16)   # RF 0x42[17:16]=0x3 arms the next measurement

TXSCALE_TABLE_SIZE = 37          # TXSCALE_TABLE_SIZE (bb_swing_idx clamp)
TXPWR_TRACK_TABLE_SIZE = 30      # delta clamp (== DELTA_SWINGIDX_SIZE)
AVG_THERMAL_NUM = 4              # AVG_THERMAL_NUM_8814A (running-average window)
RF_PATH_COUNT = 4                # MAX_PATH_NUM_8814A
THRESHOLD_IQK = 8                # config->threshold_iqk (LCK/IQK delta threshold)

# No-link tx_rate: _rtw_phydm_pwr_tracking_rate_check returns pmlmeext->tx_rate, which
# init_mlme_ext_priv_value sets to IEEE80211_CCK_RATE_1MB (== MGN_1M) for a 2.4 GHz + 11B
# interface [SRC rtw_mlme_ext.c:1058-1062]; fed to rf->p_rate_index [SRC hal_dm.c:1779].
NO_LINK_TX_RATE = 0x02           # MGN_1M / IEEE80211_CCK_RATE_1MB

# IS_CCK_RATE members [SRC ieee80211.h:911 / enum MGN_RATE].
_MGN_1M, _MGN_2M, _MGN_5_5M, _MGN_11M = 0x02, 0x04, 0x0B, 0x16


def _is_cck_rate(rate: int) -> bool:
    """[SRC] IS_CCK_RATE, ieee80211.h:911."""
    return rate in (_MGN_1M, _MGN_2M, _MGN_5_5M, _MGN_11M)


def get_delta_swing_table_8814a(channel: int, tx_rate: int) -> tuple:
    """[SRC] get_delta_swing_table_8814a, halrf_8814a_ce.c:1407 — path A/B up/down tables.

    Returns ``(up_a, down_a, up_b, down_b)``. Channel picks the band/sub-band; on 2.4 GHz the
    tx_rate picks CCK vs OFDM. Out-of-range channels fall back to the shared 8188e default.
    """
    if 1 <= channel <= 14:
        if _is_cck_rate(tx_rate):
            return (T.DELTA_SWINGIDX_2G_CCK_A_P, T.DELTA_SWINGIDX_2G_CCK_A_N,
                    T.DELTA_SWINGIDX_2G_CCK_B_P, T.DELTA_SWINGIDX_2G_CCK_B_N)
        return (T.DELTA_SWINGIDX_2GA_P, T.DELTA_SWINGIDX_2GA_N,
                T.DELTA_SWINGIDX_2GB_P, T.DELTA_SWINGIDX_2GB_N)
    if 36 <= channel <= 64:
        return (T.DELTA_SWINGIDX_5GA_P[0], T.DELTA_SWINGIDX_5GA_N[0],
                T.DELTA_SWINGIDX_5GB_P[0], T.DELTA_SWINGIDX_5GB_N[0])
    if 100 <= channel <= 144:
        return (T.DELTA_SWINGIDX_5GA_P[1], T.DELTA_SWINGIDX_5GA_N[1],
                T.DELTA_SWINGIDX_5GB_P[1], T.DELTA_SWINGIDX_5GB_N[1])
    if 149 <= channel <= 177:
        return (T.DELTA_SWINGIDX_5GA_P[2], T.DELTA_SWINGIDX_5GA_N[2],
                T.DELTA_SWINGIDX_5GB_P[2], T.DELTA_SWINGIDX_5GB_N[2])
    return (T.DELTA_SWING_TABLE_IDX_2GA_P_8188E, T.DELTA_SWING_TABLE_IDX_2GA_N_8188E,
            T.DELTA_SWING_TABLE_IDX_2GA_P_8188E, T.DELTA_SWING_TABLE_IDX_2GA_N_8188E)


def get_delta_swing_table_8814a_path_cd(channel: int, tx_rate: int) -> tuple:
    """[SRC] get_delta_swing_table_8814a_path_cd, halrf_8814a_ce.c:1491 — path C/D up/down tables.

    Same channel/rate selection as path A/B (8814A is 4T4R, so C/D need their own tables).
    """
    if 1 <= channel <= 14:
        if _is_cck_rate(tx_rate):
            return (T.DELTA_SWINGIDX_2G_CCK_C_P, T.DELTA_SWINGIDX_2G_CCK_C_N,
                    T.DELTA_SWINGIDX_2G_CCK_D_P, T.DELTA_SWINGIDX_2G_CCK_D_N)
        return (T.DELTA_SWINGIDX_2GC_P, T.DELTA_SWINGIDX_2GC_N,
                T.DELTA_SWINGIDX_2GD_P, T.DELTA_SWINGIDX_2GD_N)
    if 36 <= channel <= 64:
        return (T.DELTA_SWINGIDX_5GC_P[0], T.DELTA_SWINGIDX_5GC_N[0],
                T.DELTA_SWINGIDX_5GD_P[0], T.DELTA_SWINGIDX_5GD_N[0])
    if 100 <= channel <= 144:
        return (T.DELTA_SWINGIDX_5GC_P[1], T.DELTA_SWINGIDX_5GC_N[1],
                T.DELTA_SWINGIDX_5GD_P[1], T.DELTA_SWINGIDX_5GD_N[1])
    if 149 <= channel <= 177:
        return (T.DELTA_SWINGIDX_5GC_P[2], T.DELTA_SWINGIDX_5GC_N[2],
                T.DELTA_SWINGIDX_5GD_P[2], T.DELTA_SWINGIDX_5GD_N[2])
    return (T.DELTA_SWING_TABLE_IDX_2GA_P_8188E, T.DELTA_SWING_TABLE_IDX_2GA_N_8188E,
            T.DELTA_SWING_TABLE_IDX_2GA_P_8188E, T.DELTA_SWING_TABLE_IDX_2GA_N_8188E)


def odm_get_tracking_table(thermal_value: int, delta: int, eeprom_thermal: int,
                           channel: int, tx_rate: int, st) -> None:
    """[SRC] odm_get_tracking_table, halphyrf_ce.c:167 — fill the per-path power index arrays.

    ``delta_power_index_last[p]`` saves the previous ``delta_power_index[p]``; then, on a
    higher-than-PG thermal, both take ``up_tab[delta]``, else ``-1 * down_tab[delta]``. The
    comparison is vs ``rf->eeprom_thermal`` (the PG base), not the last tick's thermal value.
    """
    up_a, down_a, up_b, down_b = get_delta_swing_table_8814a(channel, tx_rate)
    up_c, down_c, up_d, down_d = get_delta_swing_table_8814a_path_cd(channel, tx_rate)
    up = (up_a, up_b, up_c, up_d)
    down = (down_a, down_b, down_c, down_d)
    for p in range(RF_PATH_COUNT):
        st.delta_power_index_last[p] = st.delta_power_index[p]
        if thermal_value > eeprom_thermal:
            v = up[p][delta]
        else:
            v = -1 * down[p][delta]
        st.delta_power_index[p] = v
        st.absolute_ofdm_swing_idx[p] = v


def get_mix_mode_tx_agc_bb_swing_offset(st, rf_path: int, tx_power_index_offest: int) -> None:
    """[SRC] get_mix_mode_tx_agc_bb_swing_offset, halrf_8814a_ce.c:1136 — split the absolute
    OFDM swing index into the TXAGC field (``absolute_ofdm_swing_idx``) and the BB-swing table
    index (``bb_swing_idx_ofdm``).

    Non-negative index within the TXAGC headroom stays in the TXAGC field; overflow spills the
    remnant into the BB-swing table (clamped to +10); a negative index sits entirely in the
    BB-swing table (clamped to 0). The lower-temp branch (negative index) — the only one a
    never-linked 8814A reaches — is independent of ``tx_power_index_offest``.
    """
    bb_swing_upper_bound = st.default_ofdm_index + 10
    bb_swing_lower_bound = 0
    tx_agc_index = 0
    tx_bb_swing_index = st.default_ofdm_index

    if tx_power_index_offest > 0xF:
        tx_power_index_offest = 0xF

    abs_idx = st.absolute_ofdm_swing_idx[rf_path]
    if 0 <= abs_idx <= tx_power_index_offest:
        tx_agc_index = abs_idx
        tx_bb_swing_index = st.default_ofdm_index
    elif abs_idx > tx_power_index_offest:
        tx_agc_index = tx_power_index_offest
        remnant_ofdm_swing_idx = abs_idx - tx_power_index_offest
        tx_bb_swing_index = st.default_ofdm_index + remnant_ofdm_swing_idx
        if tx_bb_swing_index > bb_swing_upper_bound:
            tx_bb_swing_index = bb_swing_upper_bound
    else:
        tx_agc_index = 0
        if st.default_ofdm_index > (abs_idx * -1):
            tx_bb_swing_index = st.default_ofdm_index + abs_idx
        else:
            tx_bb_swing_index = bb_swing_lower_bound
        if tx_bb_swing_index < bb_swing_lower_bound:
            tx_bb_swing_index = bb_swing_lower_bound

    st.absolute_ofdm_swing_idx[rf_path] = tx_agc_index
    st.bb_swing_idx_ofdm[rf_path] = tx_bb_swing_index


def power_tracking_by_mix_mode(t, st, rf_path: int, eeprom_thermal: int) -> None:
    """[SRC] power_tracking_by_mix_mode, halrf_8814a_ce.c:1190 — the per-path TXAGC + BB-swing
    register write, gated on any path's power index having moved.

    ``txpowertrack_control`` is always true off mp_mode, so only the ``power_index_offset`` and
    ``eeprom_thermal != 0xff`` guards remain.
    """
    if not (st.power_index_offset[0] or st.power_index_offset[1]
            or st.power_index_offset[2] or st.power_index_offset[3]):
        return
    if eeprom_thermal == 0xFF:
        return

    # tx_power_index headroom = 63 - phy_get_tx_power_index_8814a(...). That index function is a
    # separate subsystem deferred to a later milestone; 63 - idx clamps to 0xF for any realistic
    # index, and the lower-temp branch (the never-linked case) ignores the value entirely.
    tx_power_index_offest = 0xF
    get_mix_mode_tx_agc_bb_swing_offset(st, rf_path, tx_power_index_offest)
    if st.bb_swing_idx_ofdm[rf_path] >= TXSCALE_TABLE_SIZE:
        st.bb_swing_idx_ofdm[rf_path] = st.default_ofdm_index

    path = _PATHS[rf_path]
    _bb32(t, REG_TX_AGC[path], TXAGC_BITMASK, st.absolute_ofdm_swing_idx[rf_path])
    _bb32(t, REG_BBSWING[path], BBSWING_BITMASK,
          T.TX_SCALING_TABLE_JAGUAR[st.bb_swing_idx_ofdm[rf_path]])


def odm_tx_pwr_track_set_pwr8814a(t, st, rf_path: int, eeprom_thermal: int) -> None:
    """[SRC] odm_tx_pwr_track_set_pwr8814a, halrf_8814a_ce.c:1315 — MIX_MODE dispatch.

    ``config->odm_tx_pwr_track_set_pwr`` for 8814A. Only the MIX_MODE method (``pwt_type == 0``)
    is reached in this build; TXAGC/TSSI/BBSWING and the mixed 2G/5G methods are unused here.
    """
    power_tracking_by_mix_mode(t, st, rf_path, eeprom_thermal)


def _callback_thermal_meter(t, st, channel: int) -> None:
    """[SRC] odm_txpowertracking_callback_thermal_meter, halphyrf_ce.c:395 — read + average the
    thermal meter, then (on a change) walk the tables and apply the per-path correction.

    Ported for the no-link 8814A MIX_MODE case. Stops before the ``do_iqk_8814a`` trigger (the
    next milestone): the tail's IQK block emits its own register I/O, which the gate stops on.
    """
    tx_rate = st.p_rate_index

    # Thermal read: RF 0x42[15:10] + phydm_get_thermal_offset (0, K-free off), clamp 0..63.
    raw = _rf_read(t, "a", RF_T_METER)
    thermal_value = (raw & _THERMAL_MASK) >> 10
    thermal_value_temp = thermal_value            # + phydm_get_thermal_offset() == 0
    if thermal_value_temp > 63:
        thermal_value = 63
    elif thermal_value_temp < 0:
        thermal_value = 0
    else:
        thermal_value = thermal_value_temp

    # txpowertrack_control is true (off mp_mode); eeprom_thermal != 0xff (checked at the caller).

    # Running average over the last AVG_THERMAL_NUM samples.
    st.thermal_value_avg[st.thermal_value_avg_index] = thermal_value
    st.thermal_value_avg_index += 1
    if st.thermal_value_avg_index == AVG_THERMAL_NUM:
        st.thermal_value_avg_index = 0
    avg = avg_count = 0
    for v in st.thermal_value_avg:
        if v:
            avg += v
            avg_count += 1
    if avg_count:
        thermal_value = avg // avg_count
        st.thermal_value_delta = thermal_value - st.eeprom_thermal

    # delta / delta_lck / delta_iqk — absolute differences vs the carried thermal baselines.
    delta = abs(thermal_value - st.thermal_value)
    delta_lck = abs(thermal_value - st.thermal_value_lck)
    delta_iqk = abs(thermal_value - st.thermal_value_iqk)

    # LCK: 8814A only records thermal_value_lck (phy_lc_calibrate is IC-mask-excluded for 8814A,
    # so no register I/O) [SRC halphyrf_ce.c:548-566].
    if delta_lck >= THRESHOLD_IQK:
        st.thermal_value_lck = thermal_value

    # Move the swing table only when the thermal actually changed; delta is re-derived vs the PG
    # base (eeprom_thermal), clamped to the table size [SRC halphyrf_ce.c:577-597].
    if delta > 0:
        delta = abs(thermal_value - st.eeprom_thermal)
        if delta >= TXPWR_TRACK_TABLE_SIZE:
            delta = TXPWR_TRACK_TABLE_SIZE - 1
        odm_get_tracking_table(thermal_value, delta, st.eeprom_thermal, channel, tx_rate, st)
        for p in range(RF_PATH_COUNT):
            if st.delta_power_index[p] == st.delta_power_index_last[p]:
                st.power_index_offset[p] = 0
            else:
                st.power_index_offset[p] = (st.delta_power_index[p]
                                            - st.delta_power_index_last[p])
    else:
        for p in range(RF_PATH_COUNT):
            st.power_index_offset[p] = 0

    # 8814A MIX_MODE per-path correction (pwt_type == 0) [SRC halphyrf_ce.c:656-668].
    for p in range(RF_PATH_COUNT):
        odm_tx_pwr_track_set_pwr8814a(t, st, p, st.eeprom_thermal)

    # Record the last power-tracking thermal value [SRC halphyrf_ce.c:749].
    st.thermal_value = thermal_value

    # do_iqk_8814a trigger [SRC halphyrf_ce.c:782-793], AFTER the thermal_value store-back: on a
    # thermal swing >= THRESHOLD_IQK vs the last IQK, re-run the calibrate. The no-link gates
    # (is_scan_in_process / rfk_forbidden / is_iqk_in_progress) are all constant-false in this
    # monitor build; the caller sets thermal_value_iqk (do_iqk_8814a re-affirms it).
    if not st.rfk_forbidden and not st.is_iqk_in_progress:
        if delta_iqk >= THRESHOLD_IQK:
            st.thermal_value_iqk = thermal_value
            iqk.do_iqk_8814a(t, st, channel)


def clear_txpowertracking_state(st) -> None:
    """[SRC] odm_clear_txpowertracking_state, halphyrf_ce.c:130 — reset the per-tick tracking
    baseline to the current ``default_ofdm_index`` and the thermal base to the EFUSE value.

    Software only (no register I/O). Does NOT touch the running thermal average or
    ``thermal_value_iqk`` / ``thermal_value_lck``. Runs on every channel set (see
    ``on_channel_switch``), so each hop restarts the swing table from the band's base.
    """
    st.thermal_value = st.eeprom_thermal
    for p in range(RF_PATH_COUNT):
        st.bb_swing_idx_ofdm_base[p] = st.default_ofdm_index
        st.bb_swing_idx_ofdm[p] = st.default_ofdm_index
        st.power_index_offset[p] = 0
        st.delta_power_index[p] = 0
        st.delta_power_index_last[p] = 0
        st.absolute_ofdm_swing_idx[p] = 0


def _band_of(channel: int) -> int:
    """2.4 GHz (BAND_ON_2_4G) for ch<=14, else 5 GHz (BAND_ON_5G)."""
    return C.BAND_ON_2_4G if channel <= 14 else C.BAND_ON_5G


def on_channel_switch(st, old_channel: int, new_channel: int) -> None:
    """[SRC] phy_SwChnlAndSetBwMode8814A:2110 (clear on every set) + phy_SetBBSwingByBand:1128
    (band-switch default_ofdm_index adjust). Called after each channel tune; software only.

    A 2.4 GHz<->5 GHz crossing moves ``default_ofdm_index`` by ``BBDiffBetweenBand*2`` (each
    TxScale index is 0.5 dB), so the swing table re-bases to the new band's BB-swing before the
    per-hop clear seeds the bases from it.
    """
    old_band = _band_of(old_channel)
    new_band = _band_of(new_channel)
    if new_band != old_band:
        bb_diff_between_band = st.bb_swing_diff_2g - st.bb_swing_diff_5g
        if new_band != C.BAND_ON_2_4G:
            bb_diff_between_band = -1 * bb_diff_between_band
        st.default_ofdm_index += bb_diff_between_band * 2
        # A 2.4G<->5G crossing is exactly when phy_SwBand8814A commits current_band_type to the
        # target band (the IQK's *dm->band_type); a same-band hop leaves it lagging (BAND_MAX
        # until the first crossing). Mirrors chan.phy_sw_band's HW-marker decision.
        st.current_band_type = new_band
    clear_txpowertracking_state(st)


def txpowertracking_check_ce(t, st, channel: int) -> None:
    """[SRC] odm_txpowertracking_check_ce, halrf_powertracking_ce.c:816 — the two-phase gate.

    First call (``tm_trigger == 0``) arms the RF thermal meter (0x42[17:16]=0x3) and returns;
    the next call (``tm_trigger == 1``) runs the callback and re-clears the trigger.
    """
    if not st.tm_trigger:
        set_rf_masked(t, "a", RF_T_METER, _THERMAL_TRIGGER, 0x3)
        st.tm_trigger = 1
        return
    _callback_thermal_meter(t, st, channel)
    st.tm_trigger = 0
