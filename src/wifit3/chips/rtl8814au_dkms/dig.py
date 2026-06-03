"""RTL8814AU runtime DIG/AGC watchdog (M3c) — vendor faithful, no-link path.

Ports the phydm DIG watchdog [SRC phydm_dig.c phydm_dig] for the always-monitor
(never-linked) case: read the false-alarm (FA) counters, step the initial gain index
(IGI) toward fewer false alarms, clamp to the no-link bounds, and write it to all
four RF paths. Run every ~2 s (the kernel DIG-watchdog cadence). This adapts the M3a
DIG *seed* to the live RF environment — the long-session breadth/stability payoff of
the re-port. It only reads FA counters and writes the RX gain (no TX), so it is
passive.

Scope — the no-link monitor path only. wifit3 never associates, so the vendor DIG's
linked / DFS / TDMA / damping branches do not apply, and the diagnostic FA counters
(CRC32 / CCA / EVM) that phydm logs but does not feed into the IGI decision are not
read — only `cnt_all` (= OFDM-FA + CCK-FA), which is all `phydm_get_new_igi` consumes.
The IGI is clamped to the no-link range [0x1c, 0x2a], so an over- or under-count can
only nudge gain within a safe band — it can never drive RX deaf. Validated live (the
adaptive breadth A/B), not via the byte-for-byte differ.
"""
from __future__ import annotations

from .bb import _set_reg_masked as _bb32

# FA counters [SRC phydm_fa_cnt_statistics_ac + phydm_regdefine11ac.h].
_REG_OFDM_FA = 0x0F48          # OFDM false-alarm count (low 16 bits)
_REG_CCK_FA = 0x0A5C           # ODM_REG_CCK_FA_11AC (low 16 bits)
_REG_BB_RX_PATH = 0x0808       # ODM_REG_BB_RX_PATH_11AC; BIT28 = CCK enabled (2.4G)
_CCK_ENABLE_BIT = 1 << 28

# Per-path IGI registers + field mask [SRC phydm_write_dig_reg_c50 / regdefine11ac].
_REG_IGI = (0x0C50, 0x0E50, 0x1850, 0x1A50)
_IGI_MASK = 0x7F

# No-link (always-monitor) DIG parameters [SRC phydm_dig.c]:
#   fa_th  = {2000, 4000, 5000}  phydm_fa_threshold_check (!linked, !dfs)
#   step   = {+2, +1, -2}        phydm_new_igi_by_fa (!linked)
#   bounds = [0x1c, 0x2a]        rx_gain_range_{min,max} for no-link: DIG_MIN_COVERAGE
#                                .. DIG_MAX_OF_MIN_BALANCE_MODE (matches [WIRE] 0x1c..0x2a)
_FA_TH = (2000, 4000, 5000)
_IGI_MIN = 0x1C
_IGI_MAX = 0x2A
WATCHDOG_PERIOD_S = 2.0        # kernel DIG-watchdog cadence


def _read_fa_cnt(t) -> int:
    """[SRC] phydm_fa_cnt_statistics_ac — cnt_all = OFDM-FA (+ CCK-FA on 2.4G)."""
    ofdm_fa = t.read16(_REG_OFDM_FA)
    cck_fa = t.read16(_REG_CCK_FA)
    cck_enabled = bool(t.read32(_REG_BB_RX_PATH) & _CCK_ENABLE_BIT)
    return ofdm_fa + cck_fa if cck_enabled else ofdm_fa


def _reset_fa_cnt(t) -> None:
    """[SRC] phydm_false_alarm_counter_reg_reset (11AC) — pulse OFDM + CCK FA reset."""
    _bb32(t, 0x09A4, 1 << 17, 1)   # OFDM FA reset
    _bb32(t, 0x09A4, 1 << 17, 0)
    _bb32(t, 0x0A2C, 1 << 15, 0)   # CCK FA reset
    _bb32(t, 0x0A2C, 1 << 15, 1)


def _new_igi_by_fa(igi: int, fa_cnt: int) -> int:
    """[SRC] phydm_new_igi_by_fa with the not-linked step {+2, +1, -2}."""
    if fa_cnt > _FA_TH[2]:
        return igi + 2
    if fa_cnt > _FA_TH[1]:
        return igi + 1
    if fa_cnt < _FA_TH[0]:
        return igi - 2
    return igi


def watchdog_tick(t) -> int:
    """One DIG watchdog iteration; returns the IGI now in force (for logging).

    [SRC] phydm_dig: read the current IGI, read+reset the FA counters, pick a new IGI
    by FA, clamp to the no-link range, and (only if changed) write it to all paths
    via odm_write_dig -> phydm_write_dig_reg_c50.
    """
    igi = t.read32(_REG_IGI[0]) & _IGI_MASK
    fa_cnt = _read_fa_cnt(t)
    _reset_fa_cnt(t)
    new_igi = _new_igi_by_fa(igi, fa_cnt)
    new_igi = max(_IGI_MIN, min(_IGI_MAX, new_igi))
    if new_igi != igi:
        for reg in _REG_IGI:
            _bb32(t, reg, _IGI_MASK, new_igi)
    return new_igi
