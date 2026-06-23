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
from .rf import write_rf, write_rf_masked

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

# --- phydm_adaptivity_init (11AC, not PWDB_EDCCA) [SRC] phydm_adaptivity.c ---
R_0x944 = 0x0944                   # rx-source select for EDCCA [29:28]
R_0x8a4 = 0x08A4                   # EDCCA L2H[7:0] / H2L[15:8] threshold
R_0x520 = 0x0520                   # MAC: ignore-EDCCA bit15
R_0x524 = 0x0524                   # MAC: EDCCA count-down bit11
_EDCCA_NOLINK_TH = 0x7F            # phydm_set_edcca_threshold(0x7f, 0x7f) resume-to-no-link

# --- phydm_ra_info_init [SRC] phydm_rainfo.c --------------------------------
R_0x440 = 0x0440                   # RRSR (rrsr_val_init read)
# ARFR fallback tables for rate_id 16/18 (PHYDM_IC_RATEID_IDX_TYPE2) [SRC] phydm_arfr_table_init
_ARFR_WRITES = ((0x0494, 0xFE01F015), (0x0498, 0x40000000),
                (0x04A4, 0x003FF015), (0x04A8, 0x40000000))

# --- phydm_cfo_tracking_init [SRC] phydm_cfotracking.c ----------------------
R_0x10 = 0x0010                    # crystal-cap control-by-WiFi bit6

# --- phydm_rf_init (get_swing_index) + phydm_dc_cancellation [SRC] -----------
R_0xc1c = 0x0C1C                   # OFDM TX BB-swing (get_swing_index read)
R_0x198c = 0x198C                  # BB dbg-port clock enable [2:0]
R_0x8fc = 0x08FC                   # BB dbg-port index select
R_0xfa0 = 0x0FA0                   # BB dbg-port read-back value (11AC)
R_0x8f8 = 0x08F8                   # BB dbg-port header select [25:22]
R_0x522 = 0x0522                   # MAC TX-queue pause bitmap (1 byte)
R_0x838 = 0x0838                   # OFDM RX-CCA disable bit1
R_0xa04 = 0x0A04                   # CCK Tx-path enable [31:28]
R_0xc00 = 0x0C00                   # path-A 3-wire ctrl [3:0]
R_0xe00 = 0x0E00                   # path-B 3-wire ctrl [3:0]
R_0xa78 = 0x0A78                   # CCK DCNF disable (MASKBYTE1)
R_0x8b4 = 0x08B4                   # ck320 stop bit6
R_0xc10 = 0x0C10                   # path-A DC-cancel I (offset)
R_0xc14 = 0x0C14                   # path-A DC-cancel Q (offset)
R_0xa0c = 0x0A0C                   # CCK IGI for new-CCK-AGC (cck_new_agc only)


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


def _adaptivity_init(t, info, st: DmState) -> None:
    """phydm_adaptivity_init [SRC] phydm_adaptivity.c (PHYDM_SUPPORT_ADAPTIVITY, CE path).
    ``phydm_set_l2h_th_ini`` only sets a software value here; 8821C is 11AC and not
    ODM_IC_PWDB_EDCCA, so the RX-source select writes 0x944[29:28]=1, then the no-link EDCCA
    threshold (``phydm_set_edcca_threshold(0x7f, 0x7f)`` -> 0x8a4 L2H[7:0]/H2L[15:8]) and the MAC
    don't-ignore-EDCCA state (0x520[15]=0, 0x524[11]=1). ``phydm_set_forgetting_factor`` /
    ``phydm_edcca_decision_opt`` are PHYDM_EDCCA_ADAPT_MODE-only — this build's edcca_mode is
    'normal', so both early-return (no register I/O), matching the wire. ``phydm_enhance_monitor_init``
    before this is IFS-CLM, not in PHYDM_IC_SUPPORT_IFS_CLM for 8821C (silent)."""
    set_bb_reg(t, R_0x944, (1 << 29) | (1 << 28), 0x1)
    set_bb_reg(t, R_0x8a4, 0x00FF, _EDCCA_NOLINK_TH)
    set_bb_reg(t, R_0x8a4, 0xFF00, _EDCCA_NOLINK_TH)
    set_bb_reg(t, R_0x520, 1 << 15, 0)
    set_bb_reg(t, R_0x524, 1 << 11, 1)


def _ra_info_init(t, info, st: DmState) -> None:
    """phydm_ra_info_init [SRC] phydm_rainfo.c — latch the RRSR init value (0x440) then load the
    ARFR fallback tables (`phydm_arfr_table_init`, 8821C is PHYDM_IC_RATEID_IDX_TYPE2: rate_id
    16 -> 0x494/0x498, rate_id 18 -> 0x4a4/0x4a8). `phydm_rate_adaptive_mask_init` is software."""
    t.read32(R_0x440)
    for addr, val in _ARFR_WRITES:
        set_bb_reg(t, addr, _MASKDWORD, val)


def _cfo_tracking_init(t, info, st: DmState) -> None:
    """phydm_cfo_tracking_init [SRC] phydm_cfotracking.c — crystal-cap bookkeeping is software;
    for 8821C the only register touch is putting crystal-cap control under WiFi (0x10[6]=1).
    `phydm_rssi_monitor_init` before this is pure software."""
    set_bb_reg(t, R_0x10, 0x40, 0x1)


# --- BB debug-port primitives [SRC] phydm_debug.c (11AC), reused by IQK -------
def _bb_dbg_port_clock_en(t, enable: bool) -> None:
    """phydm_bb_dbg_port_clock_en — gate the dbg-port clock (0x198c[2:0]=7/0)."""
    set_bb_reg(t, R_0x198c, 0x7, 0x7 if enable else 0)


def _set_bb_dbg_port(t, debug_port: int) -> None:
    """phydm_set_bb_dbg_port (11AC) — enable the clock then select the dbg-port index (0x8fc).
    The priority gate (curr > pre_dbg_priority) is always satisfied here: every call in this flow
    is preceded by a release that resets pre_dbg_priority to DBGPORT_RELEASE."""
    _bb_dbg_port_clock_en(t, True)
    set_bb_reg(t, R_0x8fc, _MASKDWORD, debug_port)


def _get_bb_dbg_port_val(t) -> int:
    """phydm_get_bb_dbg_port_val (11AC) — read the dbg-port value at 0x0fa0."""
    return t.read32(R_0xfa0)


def _bb_dbg_port_header_sel(t, header_idx: int) -> None:
    """phydm_bb_dbg_port_header_sel (11AC) — select the dbg-port header (0x8f8[25:22])."""
    set_bb_reg(t, R_0x8f8, 0x3C00000, header_idx)


def _release_bb_dbg_port(t) -> None:
    """phydm_release_bb_dbg_port — disable the clock then reset the header select."""
    _bb_dbg_port_clock_en(t, False)
    _bb_dbg_port_header_sel(t, 0)


def _stop_3_wire(t, revert: bool) -> None:
    """phydm_stop_3_wire (11AC) [SRC] phydm_api.c — stop (0x4) / restart (0x7) the path-A/B
    3-wire RF interface (0xc00/0xe00[3:0])."""
    val = 0x7 if revert else 0x4
    set_bb_reg(t, R_0xc00, 0xF, val)
    set_bb_reg(t, R_0xe00, 0xF, val)


def _stop_ck320(t, enable: bool) -> None:
    """phydm_stop_ck320 (11AC) [SRC] phydm_api.c — stop/run the ck320 clock (0x8b4[6])."""
    set_bb_reg(t, R_0x8b4, 1 << 6, 1 if enable else 0)


def _write_dig(t, st: DmState, new_igi: int) -> None:
    """odm_write_dig -> phydm_write_dig_reg_c50 [SRC] phydm_dig.c — set path-A IGI (0xc50[6:0]),
    only when it changes. For new-CCK-AGC cards it also sets 0xa0c[13:8]=igi>>1; this card reads
    cck_new_agc=False so that is skipped. Adaptivity-mode IGI clamping is off (edcca_mode normal)."""
    if st.cur_ig_value == new_igi:
        return
    if st.cck_new_agc:
        set_bb_reg(t, R_0xa0c, 0x3F00, new_igi >> 1)
    set_bb_reg(t, R_0xc50, _MASK_IGI, new_igi)
    st.cur_ig_value = new_igi


def _lna_setting(t, *, enable: bool) -> None:
    """halrf_rf_lna_setting_8821c [SRC] halrf_8821c.c:28 — path-A RF-reg sequence to enable/disable
    the LNA during DC estimation. Only the two RF 0x3f writes differ between enable and disable."""
    g1, g2 = (0x1AFCE, 0x0281D) if enable else (0x0AFCE, 0x0280D)
    write_rf_masked(t, 0xEF, 1 << 19, 0x1)
    write_rf(t, 0x33, 0x00003)
    write_rf(t, 0x3E, 0x00064)
    write_rf(t, 0x3F, g1)
    write_rf_masked(t, 0xEF, 1 << 19, 0x0)
    write_rf_masked(t, 0xEE, 1 << 12, 0x1)
    write_rf(t, 0x33, 0x00003)
    write_rf(t, 0x3E, 0x00064)
    write_rf(t, 0x3F, g2)
    write_rf_masked(t, 0xEE, 1 << 12, 0x0)


def _rf_init(t, info, st: DmState) -> None:
    """phydm_rf_init [SRC] halphyrf_ce.c:1152 -> odm_txpowertracking_init. Almost all software
    (thermal-meter/swing bookkeeping); the only register touch on 8821C is ``get_swing_index``
    reading the OFDM TX BB-swing (0xc1c[31:21]) to seed the default swing index."""
    t.read32(R_0xc1c)


def _dc_cancellation(t, info, st: DmState) -> None:
    """phydm_dc_cancellation [SRC] phydm.c (PHYDM_DC_CANCELLATION; 8821C in
    ODM_DC_CANCELLATION_SUPPORT, 20 MHz so it runs; 1T1R = path-A only). Measure the path-A DC
    offset on the BB debug port with TRX stopped, LNA off and 3-wire halted, then write the
    compensation. The measured dbg-port value (recorded on the wire) drives the 0xc10/0xc14 fields.

    Phases: stop-TRX -> stop-3-wire/LNA-off -> measure -> restore -> DC compensation.
    """
    # phydm_stop_ic_trx(SET): wait BB idle on the dbg port, pause TX, kill OFDM/CCK RX
    _set_bb_dbg_port(t, 0x0)
    _get_bb_dbg_port_val(t)                  # idle when (BIT17|BIT3)==0 — true on the first read
    _release_bb_dbg_port(t)
    tx_queue_bitmap = t.read8(R_0x522)
    set_bb_reg(t, R_0x520, 0xFF0000, 0xFF)   # pause all TX queues
    set_bb_reg(t, R_0x838, 1 << 1, 1)        # disable OFDM RX CCA
    ccktx_path = (t.read32(R_0xa04) & 0xF0000000) >> 28   # phydm_dis_cck_trx(SET)
    set_bb_reg(t, R_0x808, 1 << 28, 0)       # disable CCK block
    set_bb_reg(t, R_0xa04, 0xF0000000, 0)    # disable CCK Tx

    _write_dig(t, st, 0x7E)                  # raise IGI for the measurement
    _lna_setting(t, enable=False)
    _stop_3_wire(t, revert=False)

    # Set dbg port to 0x200 (DC estimation read), disable CCK DCNF, latch the offset
    _set_bb_dbg_port(t, 0x200)
    _bb_dbg_port_header_sel(t, 0x0)
    set_bb_reg(t, R_0xa78, 0xFF00, 0x0)      # disable CCK DCNF
    _stop_ck320(t, True)
    reg_value32 = _get_bb_dbg_port_val(t)    # the measured DC offset
    _stop_ck320(t, False)
    _release_bb_dbg_port(t)

    # Restore: 3-wire, LNA, IGI, then phydm_stop_ic_trx(REVERT)
    _stop_3_wire(t, revert=True)
    _lna_setting(t, enable=True)
    _write_dig(t, st, 0x20)
    t.write8(R_0x522, tx_queue_bitmap)       # release TX queues
    set_bb_reg(t, R_0x838, 1 << 1, 0)        # enable OFDM RX CCA
    set_bb_reg(t, R_0x808, 1 << 28, 1)       # phydm_dis_cck_trx(REVERT): enable CCK block
    set_bb_reg(t, R_0xa04, 0xF0000000, ccktx_path)

    # DC compensation to the CCK data path (8821C/8822B field layout, path A)
    set_bb_reg(t, R_0xa9c, 1 << 20, 0x1)
    offset_i = 0x400 - ((reg_value32 & 0xFFC00) >> 10)
    offset_q = 0x400 - (reg_value32 & 0x3FF)
    set_bb_reg(t, R_0xc10, 0x3C000000, (0x3C0 & offset_i) >> 6)
    set_bb_reg(t, R_0xc10, 0xFC00, 0x3F & offset_i)
    set_bb_reg(t, R_0xc14, 0x3C000000, (0x3C0 & offset_q) >> 6)
    set_bb_reg(t, R_0xc14, 0xFC00, 0x3F & offset_q)


def phy_init_haldm(t, info) -> DmState:
    """rtl8821c_phy_init_haldm [SRC] rtl8821c_dm.c:174 -> rtw_phydm_init -> odm_dm_init. The
    8821C path of ``odm_dm_init``, wire-touching sub-inits only, in capture order. Returns the
    accumulated ``DmState`` (callers downstream — channel tune — may need it)."""
    st = DmState()
    _common_info_self_init(t, info, st)
    _dig_init(t, info, st)
    _cck_pd_init(t, info, st)
    _env_monitor_init(t, info, st)
    _adaptivity_init(t, info, st)
    _ra_info_init(t, info, st)
    _cfo_tracking_init(t, info, st)
    _rf_init(t, info, st)
    _dc_cancellation(t, info, st)
    return st
