"""RTL8821AU (DKMS) M5 §2: InitHalDm — phydm DIG/AGC/EDCCA seed + DIG watchdog.

Ports `rtl8812_InitHalDm` (rtl8812a_dm.c:213) -> `odm_dm_init` (phydm.c:1786) for the
8821a, always-monitor case. Seeds the dynamic-mechanism state the runtime DIG/AGC
watchdog then adapts: GPIO, the CCK/rx-path self-info reads, the DIG initial-gain
read, CCK-PD, the NHM env-monitor thresholds, the AGC RX-gain (LNA) page, the 8821a
EDCCA lower-bound search, and the RX init-gain commit.

The EDCCA search (`phydm_adaptivity_init` -> `phydm_search_pwdb_lower_bound`,
adaptivity.c:237) is **8821a/8812a-only** (ODM_IC_PWDB_EDCCA) and reads the live PSD
debug port (0xFA0), so it is NOT byte-replayable — it is gated behind `search_edcca`
(on for live hardware, off for the replay-diff acceptance gate) and verified live by
the beacon count, not the differ. Everything else is deterministic and replay-diffed.

The watchdog (`phydm_dig`, phydm_dig.c) is the runtime payoff: every ~2 s it steps the
initial gain (IGI) toward fewer false alarms within the no-link clamp. It is the
single-path (path-A 0xC50) form of the 8814au sibling's four-path watchdog — the
8821au is 1T1R. RX-side only (reads FA counters, writes RX gain), so it is passive.
"""
from __future__ import annotations

import time
from typing import NamedTuple

from .rf import RF_PATH_A, RFREG_WRITE_MASK, query_bb, set_bb, set_rf_reg

_REG_IGI = 0x0C50          # ODM_REG(IGI_A) — initial-gain, field mask 0x7F
_IGI_MASK = 0x7F
_CCA_CAP = 14              # phydm_ccx.h NHM threshold base


# ---------------------------------------------------------------------------
# InitHalDm seed (odm_dm_init order; deterministic steps verified byte-for-byte)
# ---------------------------------------------------------------------------

def _init_gpio(t) -> None:
    """[SRC] dm_InitGPIOSetting — clear GPIOSEL_ENBT (BIT5) of REG_GPIO_MUXCFG."""
    v = t.read8(0x0040)
    t.write8(0x0040, v & ~(1 << 5))


def _common_info_self_init(t) -> None:
    """[SRC] phydm_common_info_self_init / phydm_init_cck_setting — cached BB reads.

    Reads the CCK report format (0x804) and ODM_REG(BB_RX_PATH)=0x808 (rf_path_rx).
    The driver caches these for its own state; wifit3 ignores the values, but the
    two reads are on the cold-boot wire so they are emitted for the byte-exact diff.
    """
    t.read32(0x0804)
    t.read32(0x0808)


def _dig_init(t) -> int:
    """[SRC] phydm_dig_init — read the AGC-default IGI (no write at init)."""
    return t.read32(_REG_IGI) & _IGI_MASK


def _cck_pd_init(t) -> None:
    """[SRC] phydm_cck_pd_init — 8821a CCK packet-detection level (0xA0A = 0x83)."""
    t.write8(0x0A0A, 0x83)


def _env_monitor_init(t) -> None:
    """[SRC] phydm_env_monitor_init — CCX hw-restart + NHM thresholds (IGI-derived).

    Identical in shape to rtl8814au_dkms/dig.py:_env_monitor_init; the NHM thresholds
    th[i] = ((IGI - 14) << 1) + 4*i are computed from the live 0xC50 IGI, not hardcoded.
    """
    set_bb(t, 0x0994, 0x7, 0)              # ccx hw-restart: clear bits[2:0]
    set_bb(t, 0x0994, 1 << 8, 0)           # toggle BIT8 off
    set_bb(t, 0x0994, 1 << 8, 1)           # toggle BIT8 on
    igi = t.read32(_REG_IGI) & _IGI_MASK
    th = [(((igi - _CCA_CAP) << 1) + 4 * i) & 0xFF for i in range(11)]
    t.write32(0x0998, th[0] | th[1] << 8 | th[2] << 16 | th[3] << 24)
    t.write32(0x099C, th[4] | th[5] << 8 | th[6] << 16 | th[7] << 24)
    set_bb(t, 0x09A0, 0xFF, th[8])
    set_bb(t, 0x0994, 0xFFFF0000, th[9] | th[10] << 8)
    set_bb(t, 0x0990, 0xFFFF, 0xFFFF)      # CLM setting


def _lna_setting(t, *, enable: bool) -> None:
    """[SRC] halrf_rf_lna_setting_8821a — RX-gain (LNA) page commit via RF-SIPI.

    Opens the RF gain page (RF 0xEF[19]), selects Rx mode, then writes the LNA
    on/off row (RF 0x32 = 0xfb0bb enable / 0xfb09b disable), and closes the page.
    The DISABLE/ENABLE pair brackets the EDCCA PSD search (the search drops the LNA
    to measure the noise floor, then restores it).
    """
    set_rf_reg(t, RF_PATH_A, 0xEF, 0x80000, 0x1)                 # open gain page
    set_rf_reg(t, RF_PATH_A, 0x30, RFREG_WRITE_MASK, 0x18000)    # select Rx mode
    set_rf_reg(t, RF_PATH_A, 0x31, RFREG_WRITE_MASK, 0x0002F)
    set_rf_reg(t, RF_PATH_A, 0x32, RFREG_WRITE_MASK, 0xFB0BB if enable else 0xFB09B)
    set_rf_reg(t, RF_PATH_A, 0xEF, 0x80000, 0x0)                 # close gain page


def _rx_gain_commit(t) -> None:
    """[SRC] phydm RX init-gain commit — rOFDMRxIGI (0x910[15:10]) ×5, path A.

    Same tail shape as 8814au_dkms/dig.py:_rf_gain_table (single path on the 1T1R
    8821au). The cached RRSR read (0x440) precedes it on the wire.
    """
    t.read32(0x0440)
    for val in (0xFC00, 0xEC00, 0x2C00, 0x2C00, 0x2C00):
        set_bb(t, 0x0910, 0xFC00, (val >> 10) & 0x3F)


# ---------------------------------------------------------------------------
# EDCCA lower-bound search — 8821a (ODM_IC_PWDB_EDCCA), LIVE-ONLY (reads PSD 0xFA0)
# ---------------------------------------------------------------------------

# phydm_search_pwdb_lower_bound parameters [SRC] adaptivity.c:237 + adaptivity.h.
_TH_L2H_INI = -17          # phydm_set_l2h_th_ini, ODM_RTL8821 | ODM_RTL8812
_TH_EDCCA_HL_DIFF = 7      # phydm_adaptivity_init default
_IGI_BASE = 0x32
_IGI_TARGET_DC = 0x32
_ADAPT_DC_BACKOFF = 2      # ADAPT_DC_BACKOFF for ODM_CE
_RESEARCH_IGI_UB = 0x26    # phydm_re_search_condition bound
_ADAPTIVITY_DBG_PORT = 0x209   # adaptivity_dbg_port, ODM_IC_11AC_SERIES


def _set_edcca_threshold(t, h2l: int, l2h: int) -> None:
    """[SRC] phydm_set_edcca_threshold (11AC) — 0x8A4 byte0 = L2H, byte1 = H2L."""
    set_bb(t, 0x08A4, 0x000000FF, l2h & 0xFF)
    set_bb(t, 0x08A4, 0x0000FF00, h2l & 0xFF)


def _dbg_port_read(t, port: int) -> int:
    """[SRC] phydm_set/get/release_bb_dbg_port (11AC) — one PSD debug-port sample.

    Latch the port index (0x8FC), read the live value (0xFA0), release the header
    select (0x8F8[25:22]=0). The priority gate in phydm always permits here because
    every set is release-paired, so it is elided.
    """
    set_bb(t, 0x08FC, 0xFFFFFFFF, port)
    val = t.read32(0x0FA0)
    set_bb(t, 0x08F8, 0x03C00000, 0)
    return val


def _search_pwdb_lower_bound(t) -> int:
    """[SRC] phydm_search_pwdb_lower_bound — walk the L2H threshold up until the
    EDCCA signal stops firing, finding the no-link noise-floor lower bound.

    Returns the final IGI (used by phydm_re_search_condition). Reads the live PSD
    debug port, so it never runs under the replay differ.
    """
    _lna_setting(t, enable=False)
    igi = _IGI_BASE + 30 + _TH_L2H_INI - _TH_EDCCA_HL_DIFF
    th_l2h = min(_TH_L2H_INI + (_IGI_TARGET_DC - igi), 10)
    th_h2l = th_l2h - _TH_EDCCA_HL_DIFF
    _set_edcca_threshold(t, th_h2l, th_l2h)
    time.sleep(0.030)

    is_adjust = True
    while is_adjust:
        # Wait out an in-progress CCA (debug port 0x0, BIT3), bounded to 3 retries.
        reg = _dbg_port_read(t, 0x0)
        tries = 0
        while (reg & (1 << 3)) and tries < 3:
            time.sleep(0.003)
            tries += 1
            reg = _dbg_port_read(t, 0x0)
        # Count EDCCA-signal=1 (BIT29) over 20 samples of the adaptivity port.
        tx_edcca1 = sum(1 for _ in range(20)
                        if _dbg_port_read(t, _ADAPTIVITY_DBG_PORT) & (1 << 29))
        if tx_edcca1 > 1:
            igi -= 1
            th_l2h = min(th_l2h + 1, 10)
            th_h2l = th_l2h - _TH_EDCCA_HL_DIFF
            _set_edcca_threshold(t, th_h2l, th_l2h)
            if th_l2h == 10:
                is_adjust = False
        else:
            is_adjust = False

    _lna_setting(t, enable=True)
    _set_edcca_threshold(t, 0x7F, 0x7F)        # resume to the no-link state
    return igi


def _mac_edcca_state(t) -> None:
    """[SRC] phydm_mac_edcca_state(DONT_IGNORE) — don't ignore EDCCA, enable countdown."""
    set_bb(t, 0x0520, 1 << 15, 0)
    set_bb(t, 0x0524, 1 << 11, 1)


def _adaptivity_init(t) -> None:
    """[SRC] phydm_adaptivity_init (8821a PWDB-EDCCA path) — live PSD lower-bound
    search (re-run once if the result lands below the IGI bound), then DONT_IGNORE."""
    igi = _search_pwdb_lower_bound(t)
    if igi <= _RESEARCH_IGI_UB:                # phydm_re_search_condition
        _search_pwdb_lower_bound(t)
    _mac_edcca_state(t)


def init_hal_dm(t, search_edcca: bool = True) -> None:
    """rtl8812_InitHalDm: GPIO + the phydm DIG/AGC/EDCCA seed.

    ``search_edcca`` runs the live 8821a EDCCA PSD search; the replay-diff gate
    passes ``False`` (that block reads live PSD and is verified live, not byte-wise).
    Wrap this between mac.hal_init_misc_pre (§1a) and mac.hal_init_misc_post (§1b).
    """
    _init_gpio(t)
    _common_info_self_init(t)
    _dig_init(t)
    _cck_pd_init(t)
    _env_monitor_init(t)
    if search_edcca:
        _adaptivity_init(t)
    _rx_gain_commit(t)


# ---------------------------------------------------------------------------
# Runtime DIG watchdog (phydm_dig, no-link single-path) — the long-session payoff
# ---------------------------------------------------------------------------

# FA counters [SRC] phydm_fa_cnt_statistics_ac + phydm_regdefine11ac.h.
_REG_OFDM_FA = 0x0F48          # OFDM false-alarm count (low 16 bits)
_REG_CCK_FA = 0x0A5C           # ODM_REG_CCK_FA_11AC (low 16 bits)
_REG_BB_RX_PATH = 0x0808       # ODM_REG_BB_RX_PATH_11AC; BIT28 = CCK enabled (2.4G)
_CCK_ENABLE_BIT = 1 << 28

# No-link (always-monitor) DIG parameters [SRC] phydm_dig.c:
#   fa_th  = {2000, 4000, 5000}  phydm_fa_threshold_check (!linked, !dfs)
#   step   = {+2, +1, -2}        phydm_new_igi_by_fa (!linked)
#   bounds = [0x1c, 0x2a]        rx_gain_range_{min,max} no-link
_FA_TH = (2000, 4000, 5000)
_IGI_MIN = 0x1C
_IGI_MAX = 0x2A
WATCHDOG_PERIOD_S = 2.0        # kernel DIG-watchdog cadence


class DigTick(NamedTuple):
    """One watchdog iteration's outcome (for the driver's debug log).

    The raw FA components are surfaced so a stuck (never-reset) counter is visible
    at a glance: a working 2 s window reads hundreds-to-low-thousands of FA, a
    counter that is not being cleared climbs monotonically toward the 16-bit ceiling.
    """
    igi: int        # IGI now in force after clamp (and write, if it changed)
    fa_cnt: int     # cnt_all consumed this window = OFDM-FA (+ CCK-FA on 2.4G)
    ofdm_fa: int    # raw OFDM false-alarm count (0xf48)
    cck_fa: int     # raw CCK false-alarm count (0xa5c)


def _read_fa_cnt(t):
    """[SRC] phydm_fa_cnt_statistics_ac — cnt_all = OFDM-FA (+ CCK-FA on 2.4G)."""
    ofdm_fa = t.read16(_REG_OFDM_FA)
    cck_fa = t.read16(_REG_CCK_FA)
    cck_enabled = bool(t.read32(_REG_BB_RX_PATH) & _CCK_ENABLE_BIT)
    cnt_all = ofdm_fa + cck_fa if cck_enabled else ofdm_fa
    return cnt_all, ofdm_fa, cck_fa


def _reset_fa_cnt(t) -> None:
    """[SRC] phydm_false_alarm_counter_reg_reset (11AC) — the 3-pulse FA/CCA reset.

    OFDM FA (0xf48) clears on the 0x9a4[17] pulse, CCK FA (0xa5c) on the 0xa2c[15]
    pulse, and the page-F CCA counters on the 0xb58[0] pulse. The no-link IGI
    decision consumes only the two FA counters, but all three pulses are emitted to
    match the chip's runtime reset.
    """
    set_bb(t, 0x09A4, 1 << 17, 1)   # OFDM FA reset
    set_bb(t, 0x09A4, 1 << 17, 0)
    set_bb(t, 0x0A2C, 1 << 15, 0)   # CCK FA reset
    set_bb(t, 0x0A2C, 1 << 15, 1)
    set_bb(t, 0x0B58, 1 << 0, 1)    # page-F CCA-counter reset
    set_bb(t, 0x0B58, 1 << 0, 0)


def _new_igi_by_fa(igi: int, fa_cnt: int) -> int:
    """[SRC] phydm_new_igi_by_fa with the not-linked step {+2, +1, -2}."""
    if fa_cnt > _FA_TH[2]:
        return igi + 2
    if fa_cnt > _FA_TH[1]:
        return igi + 1
    if fa_cnt < _FA_TH[0]:
        return igi - 2
    return igi


def watchdog_tick(t) -> DigTick:
    """One DIG watchdog iteration; returns the resulting :class:`DigTick`.

    [SRC] phydm_dig: read the current IGI, read+reset the FA counters, pick a new IGI
    by FA, clamp to the no-link range, and (only if changed) write it to path A
    (0xC50) — the 8821au is 1T1R, so the single-path write is the whole chip.
    """
    igi = query_bb(t, _REG_IGI, _IGI_MASK)
    fa_cnt, ofdm_fa, cck_fa = _read_fa_cnt(t)
    _reset_fa_cnt(t)
    new_igi = _new_igi_by_fa(igi, fa_cnt)
    new_igi = max(_IGI_MIN, min(_IGI_MAX, new_igi))
    if new_igi != igi:
        set_bb(t, _REG_IGI, _IGI_MASK, new_igi)
    return DigTick(new_igi, fa_cnt, ofdm_fa, cck_fa)
