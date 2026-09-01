"""RTL8822C phydm sub-inits: the tail of ``odm_dm_init`` after the RF calibrations.

Most of ``odm_dm_init``'s callees are software-only or compiled out for this chip; the ten
below are the ones that reach registers. [SRC hal/phydm/phydm.c:2025]
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .phy import MASKDWORD, get_bb_reg, set_bb_reg
from .transport import RTL8822CUTransport

CCK_PD_LV_MAX = 5
CCK_PD_LV_1 = 1
CCA_CAP = 14                # NHM threshold headroom below IGI [SRC phydm_ccx.h]

# Per (bandwidth, rx-path-count) CCK power-detect and carrier-sense fields. The build reads
# each field and the level write puts it back in the same place. [SRC phydm_cck_pd.c:937,632]
_CCK_PD_FIELDS = {
    (0, 0): ((0x1AC8, 0x000000FF), (0x1AD0, 0x0000001F)),
    (1, 0): ((0x1ACC, 0x000000FF), (0x1AD0, 0x01F00000)),
    (0, 1): ((0x1AC8, 0x0000FF00), (0x1AD0, 0x000003E0)),
    (1, 1): ((0x1ACC, 0x0000FF00), (0x1AD0, 0x3E000000)),
}
# Per-level power-detect and carrier-sense steps added to the fused defaults.
_CCK_PD_STEPS = ((0, 0), (9, 0), (12, 1), (14, 1), (17, 1))

_ARFR_TABLES = ((0x0494, 0xFE01F015), (0x0498, 0x40000000),
                (0x04A4, 0x003FF015), (0x04A8, 0x40000000))
# IFS-CLM background thresholds [SRC phydm_ccx.c:2782]
_IFS_CLM_TH_LOW = (12, 5, 2, 0)
_IFS_CLM_TH_HIGH = (64, 12, 5, 2)
_IFS_CLM_REGS = (0x1ED4, 0x1ED8, 0x1EDC, 0x1EE0)


@dataclass
class PhydmState:
    """The phydm software state these inits derive from readbacks."""
    cck_new_agc: bool = False
    rf_path_rx_enable: int = 0
    cur_ig_value: int = 0
    cck_n_rx: int = 0
    cck_bw: int = 0
    cck_pd: dict = field(default_factory=dict)
    nhm_th: tuple = ()
    pw_th_rf20: int = 0
    rrsr_val_init: int = 0
    ofdm_swing: int = 0
    eeprom_thermal: tuple = (0xFF, 0xFF)    # tssi->thermal[A/B], EFUSE 0xd0/0xd1
    power_track_type: int = 0               # rf->power_track_type, EFUSE 0xc8[7:4]


def _shift(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def common_info_self_init(t: RTL8822CUTransport, dm: PhydmState) -> None:
    """phydm_common_info_self_init. The JGR3 CCK-setting init returns before touching
    anything else. [SRC phydm.c:238, :197]"""
    dm.cck_new_agc = bool(get_bb_reg(t, 0x1A9C, 1 << 17))
    dm.rf_path_rx_enable = get_bb_reg(t, 0x0808, 0xF)


def dig_init(t: RTL8822CUTransport, dm: PhydmState) -> None:
    """phydm_dig_init: read the initial gain the AGC table left behind, then seed the
    hardware-IGI offsets. [SRC phydm_dig.c:1036, :784]"""
    dm.cur_ig_value = get_bb_reg(t, 0x1D70, 0x0000007F)
    set_bb_reg(t, 0x1E80, MASKDWORD, 0x55005500)


def _read_cck_env(t: RTL8822CUTransport, dm: PhydmState) -> None:
    dm.cck_n_rx = get_bb_reg(t, 0x1A2C, 0x00060000) + 1
    dm.cck_bw = get_bb_reg(t, 0x09B0, 0xC)


def cck_pd_init(t: RTL8822CUTransport, dm: PhydmState) -> None:
    """phydm_cck_pd_init: read the fused CCK power-detect and carrier-sense defaults, build a
    per-level table from them, then apply level 1. [SRC phydm_cck_pd.c:1761, :937]"""
    _read_cck_env(t, dm)
    reg0 = get_bb_reg(t, 0x1AC8)
    reg1 = get_bb_reg(t, 0x1ACC)
    reg2 = get_bb_reg(t, 0x1AD0)
    get_bb_reg(t, 0x1AD4)                   # read for 4SS parts, unused at 2SS
    fused = {(0, 0): (reg0 & 0xFF, reg2 & 0x1F),
             (1, 0): (reg1 & 0xFF, (reg2 >> 20) & 0x1F),
             (0, 1): ((reg0 >> 8) & 0xFF, (reg2 >> 5) & 0x1F),
             (1, 1): ((reg1 >> 8) & 0xFF, (reg2 >> 25) & 0x1F)}
    for key, (pd_base, cs_base) in fused.items():
        levels = []
        for pw_step, cs_step in _CCK_PD_STEPS:
            cs = cs_base + cs_step
            # A carrier-sense ratio landing on a reserved code is bumped to the next legal one.
            cs = {0x1B: 0x1C, 0x1D: 0x1E}.get(cs, min(cs, 0x1F))
            levels.append((pd_base + pw_step, cs))
        dm.cck_pd[key] = levels
    _set_cck_pd_lv(t, dm, CCK_PD_LV_1)


def _set_cck_pd_lv(t: RTL8822CUTransport, dm: PhydmState, level: int) -> None:
    """phydm_set_cck_pd_lv_type4. [SRC phydm_cck_pd.c:711]"""
    _read_cck_env(t, dm)
    key = (dm.cck_bw, dm.cck_n_rx - 1)
    (pd_reg, pd_mask), (cs_reg, cs_mask) = _CCK_PD_FIELDS[key]
    pd_value, cs_value = dm.cck_pd[key][level]
    set_bb_reg(t, pd_reg, pd_mask, pd_value)
    set_bb_reg(t, cs_reg, cs_mask, cs_value)


def _nhm_thresholds(igi: int) -> tuple[int, ...]:
    """The 11 NHM/FAHM bucket edges, spaced 2 dB apart from IGI minus the CCA headroom.
    [SRC phydm_ccx.c:492]"""
    base = igi - CCA_CAP
    return tuple(((base + 2 * i) << 1) & 0xFF for i in range(11))


def env_monitor_init(t: RTL8822CUTransport, dm: PhydmState) -> None:
    """phydm_env_monitor_init: restart the CCX counters, then program the NHM, CLM, FAHM and
    IFS-CLM blocks. [SRC phydm_ccx.c:3801]"""
    set_bb_reg(t, 0x1E60, 0x7, 0x0)                     # ccx_hw_restart
    set_bb_reg(t, 0x1E60, 1 << 8, 0x0)
    set_bb_reg(t, 0x1E60, 1 << 8, 0x1)

    th = _nhm_thresholds(get_bb_reg(t, 0x1D70, 0x0000007F))
    dm.nhm_th = th
    set_bb_reg(t, 0x1E44, MASKDWORD, th[0] | th[1] << 8 | th[2] << 16 | th[3] << 24)
    set_bb_reg(t, 0x1E48, MASKDWORD, th[4] | th[5] << 8 | th[6] << 16 | th[7] << 24)
    set_bb_reg(t, 0x1E5C, 0x00FF0000, th[8])
    set_bb_reg(t, 0x1E60, 0xFFFF0000, th[9] | th[10] << 8)
    dm.pw_th_rf20 = get_bb_reg(t, 0x082C, 0x3F)

    set_bb_reg(t, 0x1E40, 0x0000FFFF, 0xFFFF)           # clm_setting, full period

    th = _nhm_thresholds(get_bb_reg(t, 0x1D70, 0x0000007F))
    set_bb_reg(t, 0x1E50, MASKDWORD, th[0] | th[1] << 8 | th[2] << 16 | th[3] << 24)
    set_bb_reg(t, 0x1E54, MASKDWORD, th[4] | th[5] << 8 | th[6] << 16 | th[7] << 24)
    set_bb_reg(t, 0x1E58, 0x00FFFFFF, th[8] | th[9] << 8 | th[10] << 16)
    set_bb_reg(t, 0x1E60, 1 << 3, 0x1)                  # count OFDM packets

    set_bb_reg(t, 0x1EE4, 1 << 29, 0x0)                 # ifs_clm_restart
    set_bb_reg(t, 0x1EE4, 1 << 29, 0x1)
    for reg in _IFS_CLM_REGS:
        set_bb_reg(t, reg, 1 << 31, 0x1)
    for reg, value in zip(_IFS_CLM_REGS, _IFS_CLM_TH_LOW):
        set_bb_reg(t, reg, 0x7FFF0000, value)
    for reg, value in zip(_IFS_CLM_REGS, _IFS_CLM_TH_HIGH):
        set_bb_reg(t, reg, 0x0000FFFF, value)


def adaptivity_init(t: RTL8822CUTransport) -> None:
    """phydm_adaptivity_init: park the EDCCA thresholds at their disabled value and stop the
    MAC ignoring EDCCA. The adaptive-mode helpers return early outside EDCCA adapt mode.
    [SRC phydm_adaptivity.c:1004]"""
    set_bb_reg(t, 0x084C, 0x00FF0000, 0x7F + 0x80)
    set_bb_reg(t, 0x084C, 0xFF000000, 0x7F + 0x80)
    set_bb_reg(t, 0x0520, 1 << 15, 0x0)
    set_bb_reg(t, 0x0524, 1 << 11, 0x1)


def ra_info_init(t: RTL8822CUTransport, dm: PhydmState) -> None:
    """phydm_ra_info_init: save the RRSR default and load the auto-rate-fallback tables.
    [SRC phydm_rainfo.c:2056]"""
    dm.rrsr_val_init = get_bb_reg(t, 0x0440)
    for address, value in _ARFR_TABLES:
        set_bb_reg(t, address, MASKDWORD, value)


def rf_init(t: RTL8822CUTransport, dm: PhydmState) -> None:
    """phydm_rf_init -> odm_txpowertracking_init: read the BB swing the table left, which is
    the TX-power-tracking starting index. [SRC halphyrf_ce.c:1201, halrf_powertracking_ce.c:595]"""
    dm.ofdm_swing = get_bb_reg(t, 0x0C1C, 0xFFE00000)


def dynamic_tx_power_init(t: RTL8822CUTransport) -> None:
    """phydm_dynamic_tx_power_init: clear all 64 per-MACID TX power-offset RAM entries, then
    point response frames at offset type 0. [SRC phydm_dynamictxpower.c:376, :222]"""
    for macid in range(64):
        set_bb_reg(t, 0x1E84, MASKDWORD, 0x40000000 | (macid << 24))
    set_bb_reg(t, 0x1E84, MASKDWORD, 0x80000000)        # read enable
    set_bb_reg(t, 0x1E84, MASKDWORD, 0x00000000)        # disable read/write
    set_bb_reg(t, 0x06D8, (1 << 19) | (1 << 18), 0x0)


def la_init(t: RTL8822CUTransport) -> None:
    """phydm_la_init: half-buffer logic-analyser mode. [SRC phydm_adc_sampling.c:1882]"""
    set_bb_reg(t, 0x07CC, 1 << 30, 0x0)


def mu_rsoml_init(t: RTL8822CUTransport) -> None:
    """phydm_mu_rsoml_init: read back the OFDM TX and RX path maps.
    [SRC phydm_hal_txbf_api.c:538]"""
    get_bb_reg(t, 0x0820)
    get_bb_reg(t, 0x0824, 0x000F0000)


def dm_init(t: RTL8822CUTransport, dm: PhydmState) -> None:
    """The register-touching part of ``odm_dm_init`` after ``halrf_init``.
    [SRC phydm.c:2035-2107]"""
    common_info_self_init(t, dm)
    dig_init(t, dm)
    cck_pd_init(t, dm)
    env_monitor_init(t, dm)
    adaptivity_init(t)
    ra_info_init(t, dm)
    rf_init(t, dm)
    dynamic_tx_power_init(t)
    la_init(t)
    mu_rsoml_init(t)
