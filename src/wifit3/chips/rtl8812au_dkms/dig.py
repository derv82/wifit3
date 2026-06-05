"""RTL8812AU M5: InitHalDm — phydm DIG/AGC/LNA seed + the 2-path DIG watchdog.

Ports ``rtl8812_InitHalDm`` -> ``rtw_phydm_init`` -> ``odm_dm_init`` (phydm.c) for the
8812a, always-monitor case: GPIO, CCK-PD, the NHM env-monitor thresholds, the LNA
gain-page enable (``halrf_rf_lna_setting_8812a`` — both radios on this 2T2R part, with
8812 values RF 0x31=0x3f7ff / 0x32=0xc26bf, distinct from the 8821a's), and the OFDM
RX init-gain commit. The 8812 is in the same ODM_IC_PWDB_EDCCA family as the 8821, so
the live EDCCA lower-bound search is available (``search_edcca``); it reads the live PSD
debug port so it is off for offline checks.

The watchdog (``phydm_dig``) is the runtime payoff: every ~2 s it steps the initial gain
toward fewer false alarms within the no-link clamp. It is the **two-path** form (writes
IGI to 0xC50 path A AND 0xE50 path B) of the 8821au sibling's single-path watchdog. RX-
side only (reads FA counters, writes RX gain), so it is passive.
"""
from __future__ import annotations

import time
from typing import NamedTuple

from ..rtl88xxau_base.sipi import (
    RF_PATH_A, RF_PATH_B, RFREG_WRITE_MASK, query_bb, set_bb, set_rf_reg,
)

_REG_IGI_A = 0x0C50        # ODM_REG(IGI_A) — path-A initial gain, field mask 0x7F
_REG_IGI_B = 0x0E50        # ODM_REG(IGI_B) — path-B initial gain
_IGI_MASK = 0x7F
_CCA_CAP = 14              # phydm_ccx NHM threshold base


# --- InitHalDm seed (deterministic part of odm_dm_init) ----------------------

def _init_gpio(t) -> None:
    """[SRC] dm_InitGPIOSetting — clear GPIOSEL_ENBT (BIT5) of REG_GPIO_MUXCFG."""
    v = t.read8(0x0040)
    t.write8(0x0040, v & ~(1 << 5))


def _config_cck_rx_antenna_init(t) -> None:
    """[SRC] phydm_config_cck_rx_antenna_init (phydm_api.c) — 2SS (>1SS) CCK 2R-CCA params.

    Runs because the 8812a is 2T2R (the 1SS early-return is not taken). All five are masked
    RMWs; on this card only 0xA20[5:4] actually changes (MBC weighting -> 1).
    """
    set_bb(t, 0x0A00, 1 << 15, 0x0)               # disable ant diversity
    set_bb(t, 0x0A70, 1 << 7, 0)                  # concurrent CCA at LSB & USB
    set_bb(t, 0x0A74, 1 << 8, 0)                  # RX path diversity enable
    set_bb(t, 0x0A14, 1 << 7, 0)                  # r_en_mrc_antsel
    set_bb(t, 0x0A20, (1 << 5) | (1 << 4), 1)     # MBC weighting


def _common_info_self_init(t) -> None:
    """[SRC] phydm_common_info_self_init -> phydm_init_cck_setting + the rf_path_rx read.

    phydm_init_cck_setting reads CCK_RPT_FORMAT (0x804), runs the 2R CCK antenna init, then
    common_info reads BB_RX_PATH (0x808). cck_new_agc_chk / cck_lna_bit_num_chk /
    get_cck_rssi_table_from_reg are SW-only on the 8812a; config_cck_rx_path is skipped
    (valid_path_set is A+B, neither single-path branch).
    """
    t.read32(0x0804)                  # phydm_init_cck_setting: is_cck_high_power
    _config_cck_rx_antenna_init(t)
    t.read32(0x0808)                  # common_info: rf_path_rx_enable (BB_RX_PATH)
    # phydm_trx_antenna_setting_init: cached rf-path reads (BB_RX_PATH again + 0x80c).
    t.read32(0x0808)
    t.read32(0x080C)


def _dig_init(t) -> int:
    """[SRC] phydm_dig_init — read the AGC-default IGI (no write at init)."""
    return t.read32(_REG_IGI_A) & _IGI_MASK


def _cck_pd_init(t) -> None:
    """[SRC] phydm_cck_pd_init -> phydm_write_cck_pd_type1 (0xA0A = CCK PD threshold)."""
    t.write8(0x0A0A, 0x83)


def _env_monitor_init(t) -> None:
    """[SRC] phydm_env_monitor_init — CCX hw-restart + NHM thresholds (IGI-derived).

    BB-wide (not per-path); th[i] = ((IGI - 14) << 1) + 4*i from the live 0xC50 IGI.
    Same shape as the 8821au sibling.
    """
    set_bb(t, 0x0994, 0x7, 0)
    set_bb(t, 0x0994, 1 << 8, 0)
    set_bb(t, 0x0994, 1 << 8, 1)
    igi = t.read32(_REG_IGI_A) & _IGI_MASK
    th = [(((igi - _CCA_CAP) << 1) + 4 * i) & 0xFF for i in range(11)]
    t.write32(0x0998, th[0] | th[1] << 8 | th[2] << 16 | th[3] << 24)
    t.write32(0x099C, th[4] | th[5] << 8 | th[6] << 16 | th[7] << 24)
    set_bb(t, 0x09A0, 0xFF, th[8])
    set_bb(t, 0x0994, 0xFFFF0000, th[9] | th[10] << 8)
    set_bb(t, 0x0990, 0xFFFF, 0xFFFF)


def _lna_setting(t, *, enable: bool) -> None:
    """[SRC] halrf_rf_lna_setting_8812a (halrf_8812a_ce.c:28) — RX-gain (LNA) page commit
    via RF-SIPI, both radios (rf_type > 1T1R). HALRF_LNA_ENABLE writes RF 0x32 = 0xc26bf,
    HALRF_LNA_DISABLE writes 0xc22bf. morrownr's deterministic init runs the DISABLE form
    (it is the adaptivity/EDCCA default; the watchdog manages gain at runtime)."""
    val32 = 0xC26BF if enable else 0xC22BF
    for path in (RF_PATH_A, RF_PATH_B):
        set_rf_reg(t, path, 0xEF, 0x80000, 0x1)                  # open gain page
        set_rf_reg(t, path, 0x30, RFREG_WRITE_MASK, 0x18000)     # select Rx mode
        set_rf_reg(t, path, 0x31, RFREG_WRITE_MASK, 0x3F7FF)
        set_rf_reg(t, path, 0x32, RFREG_WRITE_MASK, val32)
        set_rf_reg(t, path, 0xEF, 0x80000, 0x0)                  # close gain page


def _rx_gain_commit(t) -> None:
    """[SRC] phydm RX init-gain commit — rOFDMRxIGI (0x910[15:10]) ×5."""
    t.read32(0x0440)
    for val in (0xFC00, 0xEC00, 0x2C00, 0x2C00, 0x2C00):
        set_bb(t, 0x0910, 0xFC00, (val >> 10) & 0x3F)


def init_hal_dm(t, search_edcca: bool = False) -> None:
    """rtl8812_InitHalDm: GPIO + the phydm DIG/AGC/LNA seed. ``search_edcca`` runs the
    live PWDB-EDCCA lower-bound search (live PSD, not offline-replayable). Wrap between
    mac.hal_init_misc_pre and mac.hal_init_misc_post."""
    _init_gpio(t)
    _common_info_self_init(t)
    _dig_init(t)
    _cck_pd_init(t)
    _env_monitor_init(t)
    _lna_setting(t, enable=False)    # phydm_adaptivity_init LNA-page commit (DISABLE form)
    _rx_gain_commit(t)


# --- Runtime DIG watchdog (phydm_dig, no-link, 2-path) -----------------------

_REG_OFDM_FA = 0x0F48          # OFDM false-alarm count (low 16 bits)
_REG_CCK_FA = 0x0A5C           # CCK false-alarm count (low 16 bits)
_REG_BB_RX_PATH = 0x0808       # BIT28 = CCK enabled (2.4G)
_CCK_ENABLE_BIT = 1 << 28
_FA_TH = (2000, 4000, 5000)
_IGI_MIN = 0x1C
_IGI_MAX = 0x2A
WATCHDOG_PERIOD_S = 2.0


class DigTick(NamedTuple):
    igi: int
    fa_cnt: int
    ofdm_fa: int
    cck_fa: int


def _read_fa_cnt(t):
    ofdm_fa = t.read16(_REG_OFDM_FA)
    cck_fa = t.read16(_REG_CCK_FA)
    cck_enabled = bool(t.read32(_REG_BB_RX_PATH) & _CCK_ENABLE_BIT)
    cnt_all = ofdm_fa + cck_fa if cck_enabled else ofdm_fa
    return cnt_all, ofdm_fa, cck_fa


def _reset_fa_cnt(t) -> None:
    """[SRC] phydm_false_alarm_counter_reg_reset (11AC) — the 3-pulse FA/CCA reset."""
    set_bb(t, 0x09A4, 1 << 17, 1)   # OFDM FA reset
    set_bb(t, 0x09A4, 1 << 17, 0)
    set_bb(t, 0x0A2C, 1 << 15, 0)   # CCK FA reset
    set_bb(t, 0x0A2C, 1 << 15, 1)
    set_bb(t, 0x0B58, 1 << 0, 1)    # page-F CCA-counter reset
    set_bb(t, 0x0B58, 1 << 0, 0)


def _new_igi_by_fa(igi: int, fa_cnt: int) -> int:
    if fa_cnt > _FA_TH[2]:
        return igi + 2
    if fa_cnt > _FA_TH[1]:
        return igi + 1
    if fa_cnt < _FA_TH[0]:
        return igi - 2
    return igi


def watchdog_tick(t) -> DigTick:
    """One DIG watchdog iteration. Reads IGI, reads+resets FA, picks a new IGI by FA,
    clamps to the no-link range, and (if changed) writes BOTH path IGI regs (2T2R)."""
    igi = query_bb(t, _REG_IGI_A, _IGI_MASK)
    fa_cnt, ofdm_fa, cck_fa = _read_fa_cnt(t)
    _reset_fa_cnt(t)
    new_igi = max(_IGI_MIN, min(_IGI_MAX, _new_igi_by_fa(igi, fa_cnt)))
    if new_igi != igi:
        set_bb(t, _REG_IGI_A, _IGI_MASK, new_igi)
        set_bb(t, _REG_IGI_B, _IGI_MASK, new_igi)
    return DigTick(new_igi, fa_cnt, ofdm_fa, cck_fa)
