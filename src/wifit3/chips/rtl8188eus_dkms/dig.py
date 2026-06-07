"""RTL8188EUS runtime phydm no-link DM watchdog — vendor faithful.

Ports the periodic ~2 s ``phydm_watchdog`` no-link (always-monitor) path [SRC] phydm.c:1823.
On the wire each fire is one contiguous EP0 run (the kernel holds the IO lock across the
timer callback). The mechanisms layer in wire order [SRC] phydm.c:1846-1878:

    odm_false_alarm_counter_statistics   FA counters + EDCCA flag   (_fa_statistics)
    phydm_dig                            step the initial gain (IGI) (_dig)
    phydm_cck_pd_th                      CCK packet-detection thresh (_cck_pd)
    phydm_adaptivity                     EDCCA L2H/H2L drive         (pending)
    phydm_primary_cca                    CCK CCA gain ramp           (pending)
    phydm_env_mntr_watchdog              NHM/CLM re-seed             (pending)

8188e is 11N (1T1R). The DM carries **software state across ticks** — the IGI
(``dig_t->cur_ig_value``) and the CCK-PD state (``cck_fa_ma`` moving average +
``cur_cck_cca_thres``) are kept in sync with the chip, not re-read each tick. So this
watchdog is stateful: the driver holds one `WatchdogState`, seeded from the InitHalDm
values, and threads it through every tick. Only FA counters and RX-gain registers are
touched (no TX), so the tick is passive.

[SRC] phydm_dig.c (odm_false_alarm_counter_statistics / phydm_dig / odm_write_dig),
phydm_cck_pd.c (phydm_cck_pd_th / phydm_cckpd / phydm_write_cck_cca_th).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from . import bb

WATCHDOG_PERIOD_S = 2.0        # kernel DIG-watchdog cadence

# --- IGI / DIG (11N) [SRC] phydm_dig.c ------------------------------------
_REG_IGI = 0x0C50              # ODM_REG(IGI_A_11N), mask 0x7f
_IGI_MASK = 0x7F
_FA_TH = (2000, 4000, 5000)    # odm_fa_threshold_check (!linked, !dfs)
_STEP = (2, 1, 2)             # phydm_dig_igi_start_value (!linked): {+2, +1, -2}
_IGI_MIN = 0x1C                # dm_dig_min (DIG_MIN_COVERAGE)
_IGI_MAX = 0x2A                # dig_max_of_min (no-link rx_gain_range_max)
_IGI_SEED = 0x20               # InitHalDm DIG seed (cur_ig_value at watchdog start)

# --- 11N FA counters [SRC] phydm_regdefine11n.h ---------------------------
_REG_OFDM_FA_T1 = 0x0CF0      # [15:0] fast_fsync, [31:16] sb_search_fail
_REG_OFDM_FA_T2 = 0x0DA0      # [15:0] ofdm_cca,   [31:16] parity_fail
_REG_OFDM_FA_T3 = 0x0DA4      # [15:0] rate_illegal,[31:16] crc8_fail
_REG_OFDM_FA_T4 = 0x0DA8      # [15:0] mcs_fail
_REG_CCK_CRC32_ERR = 0x0F84
_REG_CCK_CRC32_OK = 0x0F88
_REG_OFDM_CRC32 = 0x0F94
_REG_HT_CRC32 = 0x0F90
_REG_SC_CNT = 0x08C4          # 8188e-only: bw_lsc/usc
_REG_CCK_FA_LSB = 0x0A5C      # byte0 = cck_fail low
_REG_CCK_FA_MSB = 0x0A58      # byte3 = cck_fail high
_REG_CCK_CCA_CNT = 0x0A60

# FA-counter hold/reset bit ops [SRC] phydm_false_alarm_counter_reg_{hold,reset} (11N).
_REG_OFDM_HOLD_C = 0x0C00
_REG_OFDM_HOLD_D = 0x0D00
_REG_CCK_HOLD = 0x0A2C
_REG_OFDM_RST = 0x0C0C
_REG_CRC32_RST = 0x0F14

# EDCCA flag read via BB debug port [SRC] odm_false_alarm_counter_statistics tail.
_REG_DBG_SEL = 0x0908
_REG_DBG_VAL = 0x0DF4
_ADAPT_DBG_PORT = 0x208       # adaptivity_dbg_port (8188e); EDCCA asserted on BIT30

# --- CCK-PD [SRC] phydm_cck_pd.c ------------------------------------------
_REG_CCK_CCA = 0x0A0A         # ODM_REG(CCK_CCA) — 1-byte CCK CCA/PD threshold
_REG_CCK_CCA_DEFAULT = 0x0A08  # cck_pd_init reads a0a_default from [23:16]
CCK_FA_MA_RESET = 0xFFFF       # [SRC] phydm_cck_pd.h

# --- adaptivity / EDCCA [SRC] phydm_adaptivity.c --------------------------
_REG_ECCA_TH = 0x0C4C         # rOFDM0_ECCAThreshold (L2H byte0, H2L byte2)
_IGI_TARGET = 0x32            # adaptivity->igi_base
_TH_L2H_INI = 20             # th_l2h_ini_mode2 (no ADAPTIVITY support_ability)
_TH_EDCCA_HL_DIFF = 8         # th_edcca_hl_diff_mode2


@dataclass
class WatchdogState:
    """phydm DM state carried across ticks, kept in sync with the chip (mirrors
    dm_dig_table.cur_ig_value + dm_cckpd_table). Seeded from the InitHalDm values."""
    cur_ig_value: int                     # dig_t->cur_ig_value (IGI)
    cck_fa_ma: int = CCK_FA_MA_RESET       # cckpd_t->cck_fa_ma (moving average)
    cur_cck_cca_thres: int = 0            # cckpd_t->cur_cck_cca_thres (0xa0a)


def init_state(t) -> WatchdogState:
    """Seed the DM state from the chip's post-InitHalDm values: the CCK CCA default
    ``phydm_cck_pd_init`` reads from 0xa08[23:16], and the InitHalDm IGI seed 0x20."""
    a0a_default = (t.read32(_REG_CCK_CCA_DEFAULT) >> 16) & 0xFF
    return WatchdogState(cur_ig_value=_IGI_SEED, cur_cck_cca_thres=a0a_default)


class DigTick(NamedTuple):
    """One watchdog iteration's outcome (for the driver's debug log)."""
    igi: int
    fa_cnt: int
    ofdm_fa: int
    cck_fa: int


class _FaResult(NamedTuple):
    cnt_all: int        # FA total that drives DIG (OFDM sub-counters + CCK FA)
    ofdm_fa: int
    cck_fa: int


# --- odm_false_alarm_counter_statistics (11N) -----------------------------

def _hold_fa(t) -> None:
    """``phydm_false_alarm_counter_reg_hold`` (11N) — freeze the counters."""
    bb.set_bb_reg(t, _REG_OFDM_HOLD_C, 1 << 31, 1)   # hold page-C OFDM counter
    bb.set_bb_reg(t, _REG_OFDM_HOLD_D, 1 << 31, 1)   # hold page-D OFDM counter
    bb.set_bb_reg(t, _REG_CCK_HOLD, 1 << 12, 1)      # hold CCK CCA counter
    bb.set_bb_reg(t, _REG_CCK_HOLD, 1 << 14, 1)      # hold CCK FA counter


def _read_fa_counters(t) -> tuple[int, int]:
    """The 12 counter reads of ``odm_false_alarm_counter_statistics`` (11N + 8188e SC).
    CRC32 / SC / CCA counts are read (on the wire) but only the FA sub-counters feed the
    DIG decision. Returns ``(cnt_ofdm_fail, cnt_cck_fail)``."""
    v1 = t.read32(_REG_OFDM_FA_T1)
    fast_fsync, sb_search = v1 & 0xFFFF, (v1 >> 16) & 0xFFFF
    parity = (t.read32(_REG_OFDM_FA_T2) >> 16) & 0xFFFF
    v3 = t.read32(_REG_OFDM_FA_T3)
    rate_illegal, crc8 = v3 & 0xFFFF, (v3 >> 16) & 0xFFFF
    mcs = t.read32(_REG_OFDM_FA_T4) & 0xFFFF
    cnt_ofdm_fail = parity + rate_illegal + crc8 + mcs + fast_fsync + sb_search
    t.read32(_REG_CCK_CRC32_ERR)        # cnt_cck_crc32_error
    t.read32(_REG_CCK_CRC32_OK)         # cnt_cck_crc32_ok
    t.read32(_REG_OFDM_CRC32)           # ofdm crc32 err/ok
    t.read32(_REG_HT_CRC32)             # ht crc32 err/ok
    t.read32(_REG_SC_CNT)               # 8188e bw_lsc/usc
    cck_fail = (t.read32(_REG_CCK_FA_LSB) & 0xFF) \
        | (((t.read32(_REG_CCK_FA_MSB) >> 24) & 0xFF) << 8)
    t.read32(_REG_CCK_CCA_CNT)          # cnt_cck_cca
    return cnt_ofdm_fail, cck_fail


def _read_edcca_flag(t) -> None:
    """The BB-debug-port tail of ``odm_false_alarm_counter_statistics``: read dbg port 0
    (dbg_port0), then the adaptivity dbg port (edcca_flag on BIT30). For 11N
    ``phydm_set_bb_dbg_port`` only writes 0x908 and ``..._release`` is a no-op."""
    t.write32(_REG_DBG_SEL, 0x0)
    t.read32(_REG_DBG_VAL)
    t.write32(_REG_DBG_SEL, _ADAPT_DBG_PORT)
    t.read32(_REG_DBG_VAL)


def _reset_fa(t) -> None:
    """``phydm_false_alarm_counter_reg_reset`` (11N) — clear + resume the counters."""
    bb.set_bb_reg(t, _REG_OFDM_RST, 1 << 31, 1)
    bb.set_bb_reg(t, _REG_OFDM_RST, 1 << 31, 0)        # reset OFDM FA
    bb.set_bb_reg(t, _REG_OFDM_HOLD_D, 1 << 27, 1)
    bb.set_bb_reg(t, _REG_OFDM_HOLD_D, 1 << 27, 0)
    bb.set_bb_reg(t, _REG_OFDM_HOLD_D, 1 << 31, 0)     # update + resume page-C counter
    bb.set_bb_reg(t, _REG_OFDM_HOLD_D, 1 << 31, 0)     # update + resume page-D counter
    bb.set_bb_reg(t, _REG_CCK_HOLD, (1 << 13) | (1 << 12), 0)
    bb.set_bb_reg(t, _REG_CCK_HOLD, (1 << 13) | (1 << 12), 2)   # reset + resume CCK CCA
    bb.set_bb_reg(t, _REG_CCK_HOLD, (1 << 15) | (1 << 14), 0)
    bb.set_bb_reg(t, _REG_CCK_HOLD, (1 << 15) | (1 << 14), 2)   # reset + resume CCK FA
    bb.set_bb_reg(t, _REG_CRC32_RST, 1 << 16, 1)
    bb.set_bb_reg(t, _REG_CRC32_RST, 1 << 16, 0)        # reset CRC32 counter


def _fa_statistics(t) -> _FaResult:
    """``odm_false_alarm_counter_statistics`` (11N): hold -> read counters -> EDCCA flag
    -> reset. ``cnt_all`` (the DIG input) = the six OFDM FA sub-counters + CCK FA."""
    _hold_fa(t)
    ofdm_fa, cck_fa = _read_fa_counters(t)
    _read_edcca_flag(t)
    _reset_fa(t)
    return _FaResult(ofdm_fa + cck_fa, ofdm_fa, cck_fa)


# --- phydm_dig (no-link) --------------------------------------------------

def _new_igi_by_fa(igi: int, cnt_all: int) -> int:
    """``phydm_dig_current_igi_by_fa_th`` with the not-linked step {+2, +1, -2}."""
    if cnt_all > _FA_TH[2]:
        return igi + _STEP[0]
    if cnt_all > _FA_TH[1]:
        return igi + _STEP[1]
    if cnt_all < _FA_TH[0]:
        return igi - _STEP[2]
    return igi


def _dig(t, state: WatchdogState, cnt_all: int) -> None:
    """``phydm_dig`` no-link path: step the carried IGI by FA, clamp to the no-link
    range [dm_dig_min, dig_max_of_min], and (only if changed) write it via
    ``odm_write_dig`` (0xC50). The IGI is carried, not re-read from the chip."""
    new_igi = max(_IGI_MIN, min(_IGI_MAX, _new_igi_by_fa(state.cur_ig_value, cnt_all)))
    if new_igi != state.cur_ig_value:
        bb.set_bb_reg(t, _REG_IGI, _IGI_MASK, new_igi)
        state.cur_ig_value = new_igi


# --- phydm_cck_pd_th (no-link, 8188e old path) ----------------------------

def _cck_pd(t, state: WatchdogState, cck_fa: int) -> None:
    """``phydm_cck_pd_th`` -> ``phydm_cckpd`` (no-link): update the CCK-FA moving average,
    pick the CCK CCA/PD threshold (FA>1000 -> 0x83, FA<500 -> 0x40 sensitive), and write
    0xa0a if it changed (a write resets the moving average) [SRC] phydm_cck_pd.c:189."""
    if state.cck_fa_ma == CCK_FA_MA_RESET:
        state.cck_fa_ma = cck_fa
    else:
        state.cck_fa_ma = ((state.cck_fa_ma << 1) + state.cck_fa_ma + cck_fa) >> 2

    th = state.cur_cck_cca_thres
    if state.cck_fa_ma > 1000:
        th = 0x83
    elif state.cck_fa_ma < 500:
        th = 0x40

    if state.cur_cck_cca_thres != th:           # phydm_write_cck_cca_th
        t.write8(_REG_CCK_CCA, th)
        state.cck_fa_ma = CCK_FA_MA_RESET
    state.cur_cck_cca_thres = th


def _set_edcca_threshold(t, h2l: int, l2h: int) -> None:
    """``phydm_set_edcca_threshold`` (11N): rOFDM0_ECCA byte0 = L2H, byte2 = H2L."""
    bb.set_bb_reg(t, _REG_ECCA_TH, 0x00FF00FF, (l2h & 0xFF) | ((h2l & 0xFF) << 16))


def _adaptivity(t, state: WatchdogState) -> None:
    """``phydm_adaptivity`` no-link path (8188e: no ADAPTIVITY support_ability -> mode2
    config, adaptivity_enable=False, dynamic_link_adaptivity=False, edcca_enable=True):
    drive the EDCCA L2H/H2L threshold (0xc4c) from the carried IGI.
    th_l2h = th_l2h_ini + (igi_target - igi); th_h2l = th_l2h - hl_diff (lower bounds
    h2l_lb/l2h_lb = 0). Replaces the no-link 0x7f/0x7f seed with an active threshold."""
    igi = state.cur_ig_value
    th_l2h = _TH_L2H_INI + (_IGI_TARGET - igi)
    th_h2l = th_l2h - _TH_EDCCA_HL_DIFF
    _set_edcca_threshold(t, th_h2l, th_l2h)


def watchdog_tick(t, state: WatchdogState) -> DigTick:
    """One no-link ``phydm_watchdog`` tick (wire order [SRC] phydm.c:1846-1855):
    FA statistics -> DIG -> CCK-PD -> adaptivity. ``state`` is carried across ticks (the
    driver owns it; the chip stays in sync)."""
    fa = _fa_statistics(t)
    _dig(t, state, fa.cnt_all)
    _cck_pd(t, state, fa.cck_fa)
    _adaptivity(t, state)
    return DigTick(state.cur_ig_value, fa.cnt_all, fa.ofdm_fa, fa.cck_fa)
