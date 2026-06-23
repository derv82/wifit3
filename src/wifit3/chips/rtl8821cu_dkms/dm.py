"""RTL8821CU PHYDM dynamic-mechanism init — ``rtl8821c_phy_init_haldm``.

[SRC] hal/rtl8821c/rtl8821c_dm.c:174 ``rtl8821c_phy_init_haldm`` -> ``rtw_phydm_init``
(hal_dm.c:1594) -> ``odm_dm_init`` (phydm.c:1786). ``odm_dm_init`` calls ~35 sub-inits; on this
1T1R 8821C most are software-only state (no register I/O — not ported), and a handful write/read
BB regs. We port the wire-touching ones in the order the cold-boot capture shows.

Two traps this phase carries (vs the flat tables already done): the writes derive from chip/EFUSE
state (port the computing logic, not a transcription), and the wire order is NOT the source
call-list order (e.g. 0x19a8 is written by ``phydm_init_soft_ml_setting``, reached from
``phydm_common_info_self_init``, so it lands right after the opening CCK reads).

Wire-silent for this card, in call order before the first op (verified against source, not ported):
``halrf_init`` (every sub-fn IC-gated to non-8821C or mp_mode-gated), ``phydm_supportability_init``,
``phydm_pause_func_init``, ``phydm_rfe_init`` — none touch a register here. No IQK/calibration in
this window (triggered later, on channel-set / watchdog).
"""
from __future__ import annotations

from dataclasses import dataclass

from .bb import set_bb_reg

_MASKDWORD = 0xFFFFFFFF

# --- phydm_common_info_self_init register I/O [SRC] phydm.c:238 -------------
R_0xa9c = 0x0A9C                   # CCK new-AGC check [SRC] phydm.c phydm_cck_new_agc_chk
_BIT_CCK_NEW_AGC = 1 << 17
R_0x804 = 0x0804                   # CCK_RPT_FORMAT [SRC] phydm_regdefine11ac.h:35
_BIT_CCK_RPT_FORMAT = 1 << 16      # [SRC] phydm_regdefine11ac.h:104
R_0x808 = 0x0808                   # BB_RX_PATH [SRC] phydm_regdefine11ac.h:36
_MASK_BB_RX_PATH = 0xF             # [SRC] phydm_regdefine11ac.h:105
R_0x19a8 = 0x19A8                  # soft-ML setting [SRC] phydm_soml.c phydm_init_soft_ml_setting
_SOML_MASK = (1 << 31) | (1 << 30) | (1 << 29) | (1 << 28)
_SOML_VAL = 0xD

# --- phydm_dig_init register I/O [SRC] phydm_dig.c:980 ----------------------
R_0xc50 = 0x0C50                   # IGI_A [SRC] phydm_regdefine11ac.h:62
_MASK_IGI = 0x7F                   # [SRC] phydm_regdefine11ac.h:100

# --- phydm_cck_pd_init (8821C = CCK_PD_IC_TYPE2) [SRC] phydm_cck_pd.c --------
R_0xaaa = 0x0AAA                   # aaa_default source (extend cca) [SRC] phydm_cck_pd.c
R_0xa2c = 0x0A2C                   # r_mrx / r_cca_mrc (cck_n_rx probe)
R_0xa08 = 0x0A08                   # cck pd_th field [4:0]<<16
R_0xaa8 = 0x0AA8                   # cck cs_ratio field [4:0]<<16
_CCK_PD_LV_1 = 1
# lv -> (cs_ratio add, 2R offset, pd_th) [SRC] phydm_set_cckpd_lv_type2
_CCKPD_LV2_PARAMS = {0: (0, 0, 0x3), 1: (2, 1, 0x7), 2: (4, 3, 0xD),
                     3: (6, 4, 0xD), 4: (8, 5, 0xD)}

# --- phydm_env_monitor_init (NHM/CLM/FAHM, 11AC) [SRC] phydm_ccx.c -----------
R_0x990 = 0x0990                   # CLM period (MASKLWORD)
R_0x994 = 0x0994                   # NHM/FAHM ctrl + nhm_th[10:9] + CRC-check
R_0x998 = 0x0998                   # nhm_th[3:0]
R_0x99c = 0x099C                   # nhm_th[7:4]
R_0x9a0 = 0x09A0                   # nhm_th[8] (MASKBYTE0)
R_0x1c38 = 0x1C38                  # fahm_th[2:0]
R_0x1c78 = 0x1C78                  # fahm_th[5:3]
R_0x1c7c = 0x1C7C                  # fahm_th[7:6]
R_0x1cb8 = 0x1CB8                  # fahm_th[10:8]
_CCA_CAP = 14                      # [SRC] phydm_ccx.h:41
_CLM_PERIOD_INIT = 65535           # phydm_clm_init -> phydm_clm_setting(65535)


def get_bb_reg(t, addr: int, mask: int) -> int:
    """odm_get_bb_reg — full-dword read masked + right-shifted to the mask's lowest set bit."""
    shift = (mask & -mask).bit_length() - 1
    return (t.read32(addr) & mask) >> shift


@dataclass
class DmState:
    """PHYDM ``dm_struct`` state read/computed during ``odm_dm_init`` that later sub-inits key
    on (e.g. ``cck_new_agc`` selects CCK-PD register addresses). Filled incrementally."""
    cck_new_agc: bool = False
    is_cck_high_power: bool = False
    rf_path_rx_enable: int = 0
    cur_ig_value: int = 0


def _common_info_self_init(t, info, st: DmState) -> None:
    """phydm_common_info_self_init [SRC] phydm.c:238. ``phydm_init_cck_setting`` reads the
    CCK new-AGC flag (0xa9c) + the CCK report format (0x804); its CCK rx-antenna/path/lna/rssi
    helpers are all 1SS- or non-8821C-gated no-ops here. Then the BB_RX_PATH read (0x808) and
    ``phydm_init_soft_ml_setting`` (0x19a8). ``phydm_trx_antenna_setting_init`` is a 1SS no-op."""
    st.cck_new_agc = bool(get_bb_reg(t, R_0xa9c, _BIT_CCK_NEW_AGC))
    st.is_cck_high_power = bool(get_bb_reg(t, R_0x804, _BIT_CCK_RPT_FORMAT))
    st.rf_path_rx_enable = get_bb_reg(t, R_0x808, _MASK_BB_RX_PATH)
    set_bb_reg(t, R_0x19a8, _SOML_MASK, _SOML_VAL)


def _dig_init(t, info, st: DmState) -> None:
    """phydm_dig_init [SRC] phydm_dig.c:980 — read the current path-A IGI (initial gain). The
    big-jump-step block is 8822B/97F/92F-only; for this build CFG_DIG_DAMPING_CHK (antenna-div,
    off), PHYDM_HW_IGI (8822C, off) and the TDMA-DIG block contribute no register I/O. Phy-status
    init (``phydm_rx_phy_status_init``) before this is pure software."""
    st.cur_ig_value = get_bb_reg(t, R_0xc50, _MASK_IGI)


def _write_cck_pd_type2(t, cca_th: int, cca_th_aaa: int) -> None:
    """phydm_write_cck_pd_type2 [SRC] phydm_cck_pd.c — pd_th into 0xa08[21:16], cs_ratio into
    0xaa8[20:16]."""
    set_bb_reg(t, R_0xa08, 0x3F0000, cca_th)
    set_bb_reg(t, R_0xaa8, 0x1F0000, cca_th_aaa)


def _set_cckpd_lv_type2(t, aaa_default: int, lv: int) -> None:
    """phydm_set_cckpd_lv_type2 [SRC] phydm_cck_pd.c. cck_n_rx keys on 0xa2c BIT18 && BIT22 —
    the C ``&&`` short-circuits, so when BIT18 is clear (1R, this card) only one 0xa2c read hits
    the wire. (The lv==cur-level early-return never fires from init: cur level is CCK_PD_LV_INIT.)"""
    cck_n_rx = 2 if (get_bb_reg(t, R_0xa2c, 1 << 18) and get_bb_reg(t, R_0xa2c, 1 << 22)) else 1
    add, cs_2r_offset, pd_th = _CCKPD_LV2_PARAMS[lv]
    cs_ratio = aaa_default + add
    if cck_n_rx == 2:
        cs_ratio = cs_ratio - cs_2r_offset if cs_ratio >= cs_2r_offset else 0
    _write_cck_pd_type2(t, pd_th, cs_ratio)


def _cck_pd_init(t, info, st: DmState) -> None:
    """phydm_cck_pd_init [SRC] phydm_cck_pd.c — 8821C is CCK_PD_IC_TYPE2: latch aaa_default
    (0xaaa[4:0]) then set CCK-PD level to CCK_PD_LV_1. (phydm_dig_cckpd_coex_init is
    PHYDM_DCC_ENHANCE-gated, off for this build.)"""
    aaa_default = t.read8(R_0xaaa) & 0x1F
    _set_cckpd_lv_type2(t, aaa_default, _CCK_PD_LV_1)


def _b2d(b3: int, b2: int, b1: int, b0: int) -> int:
    """BYTE_2_DWORD — pack 4 bytes MSB-first into a u32 field value."""
    return (b3 << 24) | (b2 << 16) | (b1 << 8) | b0


def _nhm_th_background(igi_curr: int) -> list[int]:
    """NHM/FAHM BACKGROUND threshold curve [SRC] phydm_nhm/fahm_th_update_chk: th[0] =
    IGI_2_NHM_TH(igi-CCA_CAP), th[i] = th[0] + IGI_2_NHM_TH(2*i) (IGI_2_NHM_TH(x)=x<<1), as u8."""
    th0 = ((igi_curr - _CCA_CAP) << 1) & 0xFF
    return [th0] + [(th0 + ((2 * i) << 1)) & 0xFF for i in range(1, 11)]


def _ccx_hw_restart(t) -> None:
    """phydm_ccx_hw_restart [SRC] phydm_ccx.c — disable then re-arm NHM/CLM/FAHM via 0x994."""
    set_bb_reg(t, R_0x994, 0x7, 0x0)
    set_bb_reg(t, R_0x994, 1 << 8, 0x0)
    set_bb_reg(t, R_0x994, 1 << 8, 0x1)


def _nhm_init(t, st: DmState) -> None:
    """phydm_nhm_init -> phydm_nhm_th_update_chk(BACKGROUND) + phydm_nhm_set_th_reg (11AC) [SRC]
    phydm_ccx.c. Reads the live IGI to build the threshold curve, then loads 0x998/0x99c/0x9a0/
    0x994."""
    th = _nhm_th_background(get_bb_reg(t, R_0xc50, _MASK_IGI))
    set_bb_reg(t, R_0x998, _MASKDWORD, _b2d(th[3], th[2], th[1], th[0]))
    set_bb_reg(t, R_0x99c, _MASKDWORD, _b2d(th[7], th[6], th[5], th[4]))
    set_bb_reg(t, R_0x9a0, 0xFF, th[8])
    set_bb_reg(t, R_0x994, 0xFFFF0000, _b2d(0, 0, th[10], th[9]))


def _fahm_init(t, st: DmState) -> None:
    """phydm_fahm_init -> phydm_fahm_th_update_chk(BACKGROUND) + phydm_fahm_set_th_reg (AC) [SRC]
    phydm_ccx.c (8821C is in PHYDM_IC_SUPPORT_FAHM). Same threshold curve as NHM, loaded into
    0x1c38/0x1c78/0x1c7c/0x1cb8, then enables CRC32 check + denominator select on 0x994."""
    th = _nhm_th_background(get_bb_reg(t, R_0xc50, _MASK_IGI))
    set_bb_reg(t, R_0x1c38, 0xFFFFFF00, _b2d(0, th[2], th[1], th[0]))
    set_bb_reg(t, R_0x1c78, 0xFFFFFF00, _b2d(0, th[5], th[4], th[3]))
    set_bb_reg(t, R_0x1c7c, 0xFFFF0000, _b2d(0, 0, th[7], th[6]))
    set_bb_reg(t, R_0x1cb8, 0xFFFFFF00, _b2d(0, th[10], th[9], th[8]))
    set_bb_reg(t, R_0x994, 0x18, 0x3)
    set_bb_reg(t, R_0x994, 0x7000, 0x7)


def _env_monitor_init(t, info, st: DmState) -> None:
    """phydm_env_monitor_init [SRC] phydm_ccx.c — restart the CCX HW, then NHM, CLM
    (period=65535 via 0x990), and FAHM init. NHM_SUPPORT/CLM_SUPPORT/FAHM_SUPPORT all on for CE."""
    _ccx_hw_restart(t)
    _nhm_init(t, st)
    set_bb_reg(t, R_0x990, 0xFFFF, _CLM_PERIOD_INIT)        # phydm_clm_init -> clm_setting
    _fahm_init(t, st)


def phy_init_haldm(t, info) -> DmState:
    """rtl8821c_phy_init_haldm [SRC] rtl8821c_dm.c:174 -> rtw_phydm_init -> odm_dm_init. The
    8821C path of ``odm_dm_init``, wire-touching sub-inits only, in capture order. Returns the
    accumulated ``DmState`` (callers downstream — channel tune — may need it)."""
    st = DmState()
    _common_info_self_init(t, info, st)
    _dig_init(t, info, st)
    _cck_pd_init(t, info, st)
    _env_monitor_init(t, info, st)
    return st
