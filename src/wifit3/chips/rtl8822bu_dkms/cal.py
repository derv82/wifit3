"""RTL8822BU post-PHY calibration setup — the sequence after the BB/RF tables (op 9410+).

The vendor runs, in order: config_phydm_trx_mode (TX/RX path + RF mode), then IQK, LCK, the
one-time DPK, and the phydm DM init (DIG/CCK-PD — the RX-detection seed). This module ports them
gate-driven against the cold-boot capture (`verify_pcap.py`); `bringup.cold_bringup` chains them.

config_phydm_trx_mode_8822b `[SRC] phydm_hal_api8822b.c:2449` sets the 2T2R path config for normal
operation: both radios active (0xC08/0xE08 = 0x3231), the TX path (Nsts/1ss antenna map) and RX
path (MRC/antenna-weight) BB regs, an RF mode-table write+poll (RF 0xEF/0x33/0x3E/0x3F), then the
igi-toggle + ccapar re-apply. On this card tx=rx=BB_PATH_AB and the 1ss/CCK path is A; central_ch
is still 0 here, so ccapar takes col 1 (2.4G/2R) and phydm_rfe is a no-op (channel 0).
"""
from __future__ import annotations

from . import chan, sipi

BB_PATH_A, BB_PATH_B, BB_PATH_AB = 1, 2, 3
RF_0xEF, RF_0x33, RF_0x3E, RF_0x3F = 0xEF, 0x33, 0x3E, 0x3F
_FULL = sipi.RFREGOFFSETMASK          # 20-bit RF-register mask
MASKDWORD = 0xFFFFFFFF                 # full 32-bit BB write (plain write32, no RMW)

# Debug-port priorities [SRC] phydm_debug.h:317 (RELEASE=0 lowest .. PRI_3=3 highest).
DBGPORT_RELEASE, DBGPORT_PRI_1, DBGPORT_PRI_3 = 0, 1, 3


class DmState:
    """The parts of PHYDM's `struct dm_struct` whose register *writes* are computed from cached
    values, so a correct replay must carry them between the functions that set and use them.
    Seeded during the DM init (dig_init / init_cck_setting) and consumed by `dc_cancellation`:

    - cur_ig_value      `dig_t->cur_ig_value` — live IGI; the !=-guard in odm_write_dig.
    - big_jump_step1    `dig_t->big_jump_step1` = (0x8C8[3:1] read once at dig_init); drives the
                        0x8C8 big-jump write, which CANNOT be re-derived later (the write mutates
                        those very bits), so it must be latched at dig_init.
    - cck_new_agc       `dm->cck_new_agc` = 0xA9C[17]; gates the 0xA0C IGI-for-CCK write.
    - pre_dbg_priority  `dm->pre_dbg_priority`; set_bb_dbg_port only fires when priority rises.
    - tx_queue_bitmap / ccktx_path  `api->...`; saved on stop_ic_trx SET, restored on REVERT.
    """

    def __init__(self) -> None:
        self.cur_ig_value = 0
        self.big_jump_step1 = 0
        self.cck_new_agc = False
        self.pre_dbg_priority = DBGPORT_RELEASE
        self.tx_queue_bitmap = 0
        self.ccktx_path = 0


def aac_check(t) -> None:
    """[SRC] aac_check_8822b (halrf_8822b.c:424) — one-off AAC check before LCK/DM init.

    RF_A 0xC9[7:3] out of [4,7] => RF_A 0xCA[19]=0 + 0xB2[18:14]=0x6 (the replay feeds 0xC9)."""
    temp = sipi.read_rf_reg(t, sipi.RF_PATH_A, 0xC9, 0xF8)
    if temp < 4 or temp > 7:
        sipi.set_rf_reg(t, sipi.RF_PATH_A, 0xCA, 1 << 19, 0x0)
        sipi.set_rf_reg(t, sipi.RF_PATH_A, 0xB2, 0x7C000, 0x6)


def rfe_init(t) -> None:
    """[SRC] phydm_rfe_8822b_init — RFE chip-top mux + s0/s1 + in/out pin config (DM init)."""
    sipi.set_bb_reg(t, 0x0064, (1 << 29) | (1 << 28), 0x3)    # chip top mux
    sipi.set_bb_reg(t, 0x004C, (1 << 26) | (1 << 25), 0x0)
    sipi.set_bb_reg(t, 0x0040, 1 << 2, 0x1)
    sipi.set_bb_reg(t, 0x1990, 0x3F, 0x30)                    # from s0 or s1
    sipi.set_bb_reg(t, 0x1990, (1 << 11) | (1 << 10), 0x3)
    sipi.set_bb_reg(t, 0x0974, 0x3F, 0x3F)                    # input or output
    sipi.set_bb_reg(t, 0x0974, (1 << 11) | (1 << 10), 0x3)


def init_cck_setting(t, st: DmState) -> None:
    """[SRC] phydm_init_cck_setting + phydm_config_cck_rx_antenna_init (2SS) — CCK RX setup.

    cck_new_agc_chk reads 0xA9C[17] (8822b new-agc flag, latched in `st`); is_cck_high_power reads
    CCK_RPT_FORMAT (0x804); both cache software flags (the replay feeds them). cck_lna_bit_num_chk
    is a no-op on 8822b."""
    st.cck_new_agc = bool(sipi.get_bb_reg(t, 0x0A9C, 1 << 17))   # phydm_cck_new_agc_chk
    t.read32(0x0804)                                  # is_cck_high_power (CCK_RPT_FORMAT) — cached
    sipi.set_bb_reg(t, 0x0A00, 1 << 15, 0x0)          # disable ant diversity
    sipi.set_bb_reg(t, 0x0A70, 1 << 7, 0x0)           # concurrent CCA at LSB & USB
    sipi.set_bb_reg(t, 0x0A74, 1 << 8, 0x0)           # RX path diversity enable
    sipi.set_bb_reg(t, 0x0A14, 1 << 7, 0x0)           # r_en_mrc_antsel
    sipi.set_bb_reg(t, 0x0A20, (1 << 5) | (1 << 4), 0x1)   # MBC weighting


def _somlrxhp_setting(t, switch_soml: bool, channel: int, rfe_type: int,
                      is_dfs_band: bool = False) -> None:
    """[SRC] phydm_somlrxhp_setting (phydm_rtl8822b.c:347) — SoML RxHP register seed.

    With SoML on (`switch_soml`) writes 0x19a8=0xd90a0000; the dynamic RxHP 0x8cc/0x8d8 writes
    apply per channel/rfe_type. On rfe_type 3 (iFEM) at a 2.4 GHz channel both branches exclude
    the 0x8cc/0x8d8 writes, so the seed is the lone 0x19a8 write."""
    sipi.set_bb_reg(t, 0x19A8, MASKDWORD, 0xD90A0000 if switch_soml else 0x090A0000)
    if (not switch_soml) and rfe_type in (1, 6, 7, 9):
        sipi.set_bb_reg(t, 0x08CC, MASKDWORD, 0x08108000)
        sipi.set_bb_reg(t, 0x08D8, 1 << 27, 0x0)
    if channel <= 14:
        if switch_soml and rfe_type not in (3, 5, 8, 17):
            sipi.set_bb_reg(t, 0x08CC, MASKDWORD, 0x08108000)
            sipi.set_bb_reg(t, 0x08D8, 1 << 27, 0x0)
    elif channel > 35:
        if switch_soml:
            sipi.set_bb_reg(t, 0x08CC, MASKDWORD, 0x08108000)
            sipi.set_bb_reg(t, 0x08D8, 1 << 27, 0x0)
    if is_dfs_band:                                    # always-low RxHP causes DFS FRD
        sipi.set_bb_reg(t, 0x08D8, MASKDWORD, 0x29035612)
        sipi.set_bb_reg(t, 0x08CC, MASKDWORD, 0x08108492)


def init_soft_ml_setting(t, channel: int, rfe_type: int) -> None:
    """[SRC] phydm_init_soft_ml_setting (phydm_soml.c:1423) — 8822b non-MP: enable SoML RxHP.

    wifit3 is always in normal (not MP) mode, so the 8822b branch fires: somlrxhp(on)."""
    _somlrxhp_setting(t, True, channel, rfe_type)     # dm->bsomlenabled = True (software)


def common_info_self_init(t, st: DmState, rfe_type: int, channel: int = 1) -> None:
    """[SRC] phydm_common_info_self_init (phydm.c:238) — DM self-init after phydm_rfe_init.

    Only three steps touch the wire: phydm_init_cck_setting (CCK RX setup), the BB_RX_PATH read
    that caches rf_path_rx_enable (ODM_REG_BB_RX_PATH_11AC 0x808, software-only), and
    phydm_init_soft_ml_setting (SoML RxHP seed). The CE-gated debug-setting block and
    phydm_trx_antenna_setting_init (a no-op on 8822b — only 8192F/E/97F + 8812/8814A read regs
    there) emit nothing. `channel` is the cold default (2.4 GHz, ≤14)."""
    init_cck_setting(t, st)                            # phydm_init_cck_setting
    sipi.get_bb_reg(t, 0x0808, 0xF)                    # rf_path_rx_enable (BB_RX_PATH, cached)
    init_soft_ml_setting(t, channel, rfe_type)         # phydm_init_soft_ml_setting


def dig_init(t, st: DmState) -> None:
    """[SRC] phydm_dig_init (phydm_dig.c:1000) — DIG (RX IGI) seed.

    Mostly software field-setup; the only wire reads are the current IGI (phydm_get_igi path-A,
    0xC50[6:0]) into cur_ig_value, and the 8822b big-jump steps from 0x8C8[15:0]. Both are latched
    in `st`: enable_adjust_big_jump=1 and big_jump_step1=0x8C8[3:1] feed phydm_set_big_jump_step
    in odm_write_dig later (big_jump_lmt defaults to 0x64 / agc_table_idx 0)."""
    st.cur_ig_value = sipi.get_bb_reg(t, 0x0C50, 0x7F)   # phydm_get_igi(path A) -> cur_ig_value
    ret = sipi.get_bb_reg(t, 0x08C8, 0xFFFF)             # big_jump_step1/2/3 (RTL8822B)
    st.big_jump_step1 = (ret & 0xE) >> 1


def cck_pd_init(t) -> None:
    """[SRC] phydm_cck_pd_init (phydm_cck_pd.c:1698) — 8822b is CCK-PD type1 (non-MP): the only wire
    op is phydm_set_cckpd_lv_type1(CCK_PD_LV_1) writing pd_th 0x83 to CCK_CCA_TH (0xA0A)."""
    t.write8(0x0A0A, 0x83)


def env_monitor_init(t) -> None:
    """[SRC] phydm_env_monitor_init (phydm_ccx.c:3447) — NHM + CLM environment-monitor seed.

    phydm_ccx_hw_restart (11AC reg 0x994): disable NHM/CLM/FAHM then toggle BIT(8). phydm_nhm_init →
    phydm_nhm_th_update_chk(NHM_BACKGROUND) derives 11 thresholds from the live IGI
    (`th[i] = ((igi - CCA_CAP=14) << 1) + IGI_2_NHM_TH(2)*i`, IGI_2_NHM_TH(x)=x<<1) and
    phydm_nhm_set_th_reg packs them into 0x998/0x99c/0x9a0[7:0]/0x994[31:16]. phydm_clm_init →
    phydm_clm_setting(65535) writes the CLM period to 0x990[15:0]."""
    sipi.set_bb_reg(t, 0x0994, 0x7, 0x0)              # ccx_hw_restart: disable NHM/CLM/FAHM
    sipi.set_bb_reg(t, 0x0994, 1 << 8, 0x0)
    sipi.set_bb_reg(t, 0x0994, 1 << 8, 0x1)
    igi = sipi.get_bb_reg(t, 0x0C50, 0x7F)            # phydm_get_igi(path A)
    th0 = (igi - 14) << 1
    th = [(th0 + 4 * i) & 0xFF for i in range(11)]    # th_step 2 -> +IGI_2_NHM_TH(2*i) = +4*i
    sipi.set_bb_reg(t, 0x0998, 0xFFFFFFFF, th[3] << 24 | th[2] << 16 | th[1] << 8 | th[0])
    sipi.set_bb_reg(t, 0x099C, 0xFFFFFFFF, th[7] << 24 | th[6] << 16 | th[5] << 8 | th[4])
    sipi.set_bb_reg(t, 0x09A0, 0xFF, th[8])
    sipi.set_bb_reg(t, 0x0994, 0xFFFF0000, th[10] << 8 | th[9])
    sipi.set_bb_reg(t, 0x0990, 0xFFFF, 65535)         # phydm_clm_setting period
    # phydm_fahm_init: same IGI-derived thresholds (FAHM_BACKGROUND) -> 0x1c38/0x1c78/0x1c7c/0x1cb8
    # (PHYDM_IC_AC packing), then count-OFDM-pkt enable 0x994[3].
    fi = sipi.get_bb_reg(t, 0x0C50, 0x7F)
    f0 = (fi - 14) << 1
    f = [(f0 + 4 * i) & 0xFF for i in range(11)]
    sipi.set_bb_reg(t, 0x1C38, 0xFFFFFF00, f[2] << 16 | f[1] << 8 | f[0])
    sipi.set_bb_reg(t, 0x1C78, 0xFFFFFF00, f[5] << 16 | f[4] << 8 | f[3])
    sipi.set_bb_reg(t, 0x1C7C, 0xFFFF0000, f[7] << 8 | f[6])
    sipi.set_bb_reg(t, 0x1CB8, 0xFFFFFF00, f[10] << 16 | f[9] << 8 | f[8])
    sipi.set_bb_reg(t, 0x0994, 1 << 3, 1)             # phydm_fahm_init: count OFDM pkt


def adaptivity_init(t) -> None:
    """[SRC] phydm_adaptivity_init (phydm_adaptivity.c:666) — EDCCA seed (CE, 11AC, !PWDB_EDCCA).

    th_l2h_ini/th_edcca_hl_diff are software. 11AC && !PWDB_EDCCA picks the EDCCA dbg source
    (0x944[29:28]=1); the no-link resume sets the EDCCA threshold to 0x7f/0x7f
    (phydm_set_edcca_threshold -> 0x8a4 byte0=L2H, byte1=H2L); phydm_mac_edcca_state(DONT_IGNORE)
    sets 0x520[15]=0 + 0x524[11]=1. phydm_set_forgetting_factor and phydm_edcca_decision_opt are
    no-ops (edcca_mode != ADAPT_MODE)."""
    sipi.set_bb_reg(t, 0x0944, (1 << 29) | (1 << 28), 0x1)
    sipi.set_bb_reg(t, 0x08A4, 0x00FF, 0x7F)          # set_edcca_threshold L2H
    sipi.set_bb_reg(t, 0x08A4, 0xFF00, 0x7F)          # set_edcca_threshold H2L
    sipi.set_bb_reg(t, 0x0520, 1 << 15, 0x0)          # mac_edcca_state: don't ignore EDCCA
    sipi.set_bb_reg(t, 0x0524, 1 << 11, 0x1)          # mac_edcca_state: disable count-down


def ra_info_init(t) -> None:
    """[SRC] phydm_ra_info_init (phydm_rainfo.c:2034) — rate-adaptation init.

    Caches rrsr_val_init (R 0x440); 8822b sets 0x4cc[31:24] = 0x4c8[23:16] - 1; phydm_arfr_table_init
    (RATEID_IDX_TYPE2) writes the 2.4G AC 2ss/1ss auto-rate-fallback tables to 0x494/0x498/0x4a4/
    0x4a8. phydm_rate_adaptive_mask_init is software."""
    sipi.get_bb_reg(t, 0x0440, 0xFFFFFFFF)            # rrsr_val_init
    rv = sipi.get_bb_reg(t, 0x04C8, 0xFF0000)         # 0x4c8[23:16]
    sipi.set_bb_reg(t, 0x04CC, 0xFF000000, (rv - 1) & 0xFF)
    t.write32(0x0494, 0xFE01F015)                     # phydm_arfr_table_init
    t.write32(0x0498, 0x40000000)
    t.write32(0x04A4, 0x003FF015)
    t.write32(0x04A8, 0x40000000)


def cfo_tracking_init(t) -> None:
    """[SRC] phydm_cfo_tracking_init (phydm_cfotracking.c:367) — CFO-tracking seed.

    All software (crystal-cap caching) except the 8822b/8821c tail: crystal-cap is controlled
    by WiFi, so `odm_set_mac_reg(R_0x10, 0x40, 1)` sets REG_SYS_SWR_CTRL1[6]. The replay reads
    0x10 (a SYS reg ≤ 0xFF, so the 0x4E0 page-switch mirror applies — transport handles it)."""
    sipi.set_bb_reg(t, 0x0010, 0x40, 0x1)             # crystal cap control by WiFi


def rf_init(t) -> None:
    """[SRC] phydm_rf_init (halphyrf_ce.c:1152) — odm_txpowertracking_init + clear-state.

    odm_txpowertracking_thermal_meter_init calls get_swing_index, whose only 8822b wire op is the
    OFDM bb-swing read 0xc1c[31:21] (compared against tx_scaling_table_jaguar in software);
    get_cck_swing_index and odm_clear_txpowertracking_state are software-only on 8822b."""
    sipi.get_bb_reg(t, 0x0C1C, 0xFFE00000)            # get_swing_index: default OFDM bb-swing


# --- DC-cancellation BB primitives [SRC] phydm_api.c / phydm_debug.c / phydm_dig.c (11AC paths) ---

def _bb_dbg_port_clock_en(t, enable: bool) -> None:
    """[SRC] phydm_bb_dbg_port_clock_en — 11AC_2: gate the debug-port clock 0x198C[2:0]."""
    sipi.set_bb_reg(t, 0x198C, 0x7, 0x7 if enable else 0x0)


def _bb_dbg_port_header_sel(t, header_idx: int) -> None:
    """[SRC] phydm_bb_dbg_port_header_sel — 11AC: debug-port header select 0x8F8[25:22]."""
    sipi.set_bb_reg(t, 0x08F8, 0x3C00000, header_idx)


def _set_bb_dbg_port(t, st: DmState, priority: int, debug_port: int) -> bool:
    """[SRC] phydm_set_bb_dbg_port — only fires when priority rises; 11AC: clock-en + 0x8FC."""
    if priority > st.pre_dbg_priority:
        _bb_dbg_port_clock_en(t, True)
        sipi.set_bb_reg(t, 0x08FC, MASKDWORD, debug_port)
        st.pre_dbg_priority = priority
        return True
    return False


def _release_bb_dbg_port(t, st: DmState) -> None:
    """[SRC] phydm_release_bb_dbg_port — clock off + header 0 + drop priority."""
    _bb_dbg_port_clock_en(t, False)
    _bb_dbg_port_header_sel(t, 0)
    st.pre_dbg_priority = DBGPORT_RELEASE


def _get_bb_dbg_port_val(t) -> int:
    """[SRC] phydm_get_bb_dbg_port_val — 11AC: read the debug port at 0xFA0."""
    return t.read32(0x0FA0)


def _stop_3_wire(t, revert: bool) -> None:
    """[SRC] phydm_stop_3_wire — 11AC: 0xC00/0xE00[3:0] = 7 (start) or 4 (stop)."""
    v = 0x7 if revert else 0x4
    sipi.set_bb_reg(t, 0x0C00, 0xF, v)
    sipi.set_bb_reg(t, 0x0E00, 0xF, v)


def _stop_ck320(t, enable: bool) -> None:
    """[SRC] phydm_stop_ck320 — 11AC: 0x8B4[6] (1 stop / 0 start)."""
    sipi.set_bb_reg(t, 0x08B4, 1 << 6, 0x1 if enable else 0x0)


def _dis_cck_trx(t, st: DmState, revert: bool) -> None:
    """[SRC] phydm_dis_cck_trx — 11AC: save/disable then restore the CCK block + TX path."""
    if not revert:                                          # PHYDM_SET
        st.ccktx_path = sipi.get_bb_reg(t, 0x0A04, 0xF0000000)  # save CCK TX path
        sipi.set_bb_reg(t, 0x0808, 1 << 28, 0x0)            # disable CCK block
        sipi.set_bb_reg(t, 0x0A04, 0xF0000000, 0x0)         # disable CCK Tx
    else:                                                   # PHYDM_REVERT
        sipi.set_bb_reg(t, 0x0808, 1 << 28, 0x1)            # enable CCK block
        sipi.set_bb_reg(t, 0x0A04, 0xF0000000, st.ccktx_path)  # restore CCK Tx


def _stop_ic_trx(t, st: DmState, revert: bool) -> bool:
    """[SRC] phydm_stop_ic_trx (11AC) — stop/restore MAC+BB TRX around a measurement.

    SET: park the debug port at 0x0, poll it (0xFA0) until BB idle (PHYTXON BIT17 + CCA_all BIT3
    clear; the replay feeds an already-idle value so the loop breaks on read 1), pause all TX
    queues (0x520[23:16]=0xFF), kill OFDM RX CCA (0x838[1]) + the CCK block. REVERT undoes them."""
    if not revert:                                          # PHYDM_SET
        _set_bb_dbg_port(t, st, DBGPORT_PRI_3, 0x0)
        idle = False
        for _ in range(100):
            if (_get_bb_dbg_port_val(t) & ((1 << 17) | (1 << 3))) == 0:
                idle = True
                break
        _release_bb_dbg_port(t, st)
        if not idle:
            return False
        st.tx_queue_bitmap = t.read8(0x0522)                # save TX-queue pause bitmap
        sipi.set_bb_reg(t, 0x0520, 0xFF0000, 0xFF)          # pause all TX queues
        sipi.set_bb_reg(t, 0x0838, 1 << 1, 0x1)            # disable OFDM RX CCA
        _dis_cck_trx(t, st, revert=False)
        return True
    t.write8(0x0522, st.tx_queue_bitmap)                    # release all TX queues
    sipi.set_bb_reg(t, 0x0838, 1 << 1, 0x0)                # enable OFDM RX CCA
    _dis_cck_trx(t, st, revert=True)
    return True


def _set_big_jump_step(t, st: DmState, curr_igi: int) -> None:
    """[SRC] phydm_set_big_jump_step (8822b) — pick the big-jump index for curr_igi and write
    0x8C8[3:1]. step table + big_jump_lmt (0x64 default) are fixed; big_jump_step1 from dig_init."""
    step1 = (24, 30, 40, 50, 60, 70, 80, 90)
    big_jump_lmt = 0x64                                     # big_jump_lmt[agc_table_idx=0]
    i = 0
    while i <= st.big_jump_step1:                           # enable_adjust_big_jump=1 on 8822b
        if (curr_igi + step1[i]) > big_jump_lmt:
            if i != 0:
                i -= 1
            break
        if i == st.big_jump_step1:
            break
        i += 1
    sipi.set_bb_reg(t, 0x08C8, 0xE, i)


def _odm_write_dig(t, st: DmState, new_igi: int) -> None:
    """[SRC] odm_write_dig / phydm_write_dig_reg_c50 (8822b, no-link) — set IGI to new_igi.

    No-link + edcca!=ADAPT, so the only paths are: big-jump step (0x8C8), the new-CCK-AGC IGI
    0xA0C[13:8]=igi>>1 (when cck_new_agc), and the path-A/B IGI 0xC50/0xE50[6:0]."""
    if st.cur_ig_value == new_igi:
        return
    _set_big_jump_step(t, st, new_igi)
    if st.cck_new_agc:
        sipi.set_bb_reg(t, 0x0A0C, 0x3F00, new_igi >> 1)
    sipi.set_bb_reg(t, 0x0C50, 0x7F, new_igi)              # IGI path A
    sipi.set_bb_reg(t, 0x0E50, 0x7F, new_igi)              # IGI path B
    st.cur_ig_value = new_igi


def _apply_dc_offset(t, reg_i: int, reg_q: int, reg_value32: int) -> None:
    """[SRC] phydm_dc_cancellation tail (8822b) — turn one path's measured debug-port word into
    the RX DC compensation: I = 0xFA0[19:10], Q = 0xFA0[9:0], negate, split across [29:26]/[15:10]."""
    offset_i = 0x400 - ((reg_value32 & 0xFFC00) >> 10)
    offset_q = 0x400 - (reg_value32 & 0x3FF)
    sipi.set_bb_reg(t, reg_i, 0x3C000000, (0x3C0 & offset_i) >> 6)
    sipi.set_bb_reg(t, reg_i, 0xFC00, 0x3F & offset_i)
    sipi.set_bb_reg(t, reg_q, 0x3C000000, (0x3C0 & offset_q) >> 6)
    sipi.set_bb_reg(t, reg_q, 0xFC00, 0x3F & offset_q)


def dc_cancellation(t, st: DmState, rf_type: int = 2) -> None:
    """[SRC] phydm_dc_cancellation (phydm.c:3496, 8822b 20 MHz) — measure + cancel RX DC offset.

    For each path (A, then B — 8822b breaks after B): stop TRX, force IGI 0x7E, stop 3-wire,
    park the debug port (0x200 path-A / 0x202 path-B), disable CCK DCNF (0xA78[15:8]=0), stop
    ck320, read the DC word from 0xFA0, then restore everything (IGI 0x20). Finally enable the
    CCK-path DC comp (0xA9C[20]) and write the per-path I/Q offsets (0xC10/0xC14, 0xE10/0xE14).
    The replay feeds the 0xFA0 words, so the offsets reproduce byte-for-byte."""
    reg_value32 = [0, 0]
    for path in range(2):                                  # break after RF_PATH_B on 8822b
        if not _stop_ic_trx(t, st, revert=False):
            return
        _odm_write_dig(t, st, 0x7E)
        _stop_3_wire(t, revert=False)
        if not _set_bb_dbg_port(t, st, DBGPORT_PRI_1, 0x200 if path == 0 else 0x202):
            return
        _bb_dbg_port_header_sel(t, 0x0)
        sipi.set_bb_reg(t, 0x0A78, 0xFF00, 0x0)            # disable CCK DCNF
        _stop_ck320(t, True)
        reg_value32[path] = _get_bb_dbg_port_val(t)        # the measured DC word
        _stop_ck320(t, False)
        _release_bb_dbg_port(t, st)
        _stop_3_wire(t, revert=True)
        _odm_write_dig(t, st, 0x20)
        _stop_ic_trx(t, st, revert=True)
    sipi.set_bb_reg(t, 0x0A9C, 1 << 20, 0x1)              # DC comp to CCK data path
    _apply_dc_offset(t, 0x0C10, 0x0C14, reg_value32[0])    # path A
    if rf_type > 0:                                        # > RF_1T1R
        _apply_dc_offset(t, 0x0E10, 0x0E14, reg_value32[1])  # path B


# RF 0x18 sweep written per TxA-bias offset (0..0xb) [SRC] _set_tx_a_cali_value (phydm_rtl8822b.c).
_TXA_18 = (0x10124, 0x10524, 0x10924, 0x10D24, 0x30164, 0x30564,
           0x30964, 0x30D64, 0x50195, 0x50595, 0x50995, 0x50D95)
# efuse byte -> (is_minus, comp); any other value (0xF0/default) means "no calibration".
_TXA_COMP = {0xF6: (True, 3), 0xF4: (True, 2), 0xF2: (True, 1),
             0xF3: (False, 1), 0xF5: (False, 2), 0xF7: (False, 3), 0xF9: (False, 4)}


def _set_txa_cali_value(t, path: int, offset: int, efuse_value: int) -> None:
    """[SRC] _set_tx_a_cali_value (phydm_rtl8822b.c) — one TxA-bias offset: drive RF 0x18 to the
    fixed sweep value, read RF 0x61, and (only for an F2..F9 efuse byte) write the corrected bias
    to RF 0x30. On this card the PG byte is 0xF0 ("do nothing"), so it stops after the 0x61 read."""
    sipi.set_rf_reg(t, path, 0x18, _FULL, _TXA_18[offset])
    modi = sipi.read_rf_reg(t, path, 0x61, _FULL)
    comp = _TXA_COMP.get(efuse_value)
    if comp is None:                                       # 0xF0 / default -> no RF 0x30 write
        return
    is_minus, comp_value = comp
    tmp1 = modi & 0xF
    if is_minus:
        tmp1 = tmp1 - comp_value if tmp1 >= comp_value else 0
    else:
        tmp1 = min(tmp1 + comp_value, 7)
    sipi.set_rf_reg(t, path, 0x30, 0xFFFF, (offset << 12) | (modi & 0xFF0) | tmp1)


def _txa_bias_cali_path(t, path: int, efuse_value: int) -> None:
    """[SRC] _txa_bias_cali_4_each_path — RF 0xEF=0x200 (set-TxA-bias on), 12 offsets, then off."""
    sipi.set_rf_reg(t, path, 0xEF, _FULL, 0x200)
    for offset in range(12):
        _set_txa_cali_value(t, path, offset, efuse_value)
    sipi.set_rf_reg(t, path, 0xEF, _FULL, 0x0)


def tx_current_calibration(t, efuse0x3d7: int, efuse0x3d8: int, rf_type: int = 2) -> None:
    """[SRC] phydm_txcurrentcalibration (phydm_rtl8822b.c:240, 8822b) — 5G TxA-bias current cal.

    Save RF 0x18 (both paths), and unless PG efuse 0x3D7 is blank (0xFF) run the per-path TxA-bias
    sweep keyed by efuse 0x3D7 (path A) / 0x3D8 (path B), then restore RF 0x18. The replay feeds
    the RF 0x61 reads; on this card 0x3D7/0x3D8 are 0xF0, so the sweep only programs RF 0x18 and
    reads RF 0x61 (no RF 0x30 correction)."""
    orig_a = sipi.read_rf_reg(t, sipi.RF_PATH_A, 0x18, _FULL)
    orig_b = sipi.read_rf_reg(t, sipi.RF_PATH_B, 0x18, _FULL)
    if efuse0x3d7 == 0xFF:                                  # no PG -> no TxA cali
        return
    _txa_bias_cali_path(t, sipi.RF_PATH_A, efuse0x3d7)
    _txa_bias_cali_path(t, sipi.RF_PATH_B, efuse0x3d8)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, 0x18, _FULL, orig_a)   # restore 0x18
    sipi.set_rf_reg(t, sipi.RF_PATH_B, 0x18, _FULL, orig_b)


def _set_pa_bias_to_rf(t, path: int, tx_pa_bias: int) -> None:
    """[SRC] phydm_set_pa_bias_to_rf_8822b — fold the PG PA-bias into RF 0x3f[12:9].

    Read RF 0x51/0x52, reassemble the current 0x3f bias field from their bits, add the signed PG
    offset (clamp 0..7), then write it back through the RF 0x3f LUT (0xef[10] enable, the 0x33
    bank-index 0/1/2/3 sweep each followed by 0x3f). The replay feeds the 0x51/0x52 reads."""
    rf51 = sipi.read_rf_reg(t, path, 0x51, _FULL)
    rf52 = sipi.read_rf_reg(t, path, 0x52, _FULL)
    rf3f = (((rf52 & 0xE0000) >> 17)
            | (((rf52 & 0x18000) >> 15) << 3)
            | ((rf52 & 0xF) << 5)
            | (((rf51 & 0x78) >> 3) << 9)
            | (((rf52 & 0x2000) >> 13) << 13))
    bias = ((rf3f & 0x1E00) >> 9) + tx_pa_bias            # tx_pa_bias_bmask = BIT12..9
    bias = max(0, min(7, bias))
    rf3f = (rf3f & 0xFE1FF) | (bias << 9)
    sipi.set_rf_reg(t, path, 0xEF, 1 << 10, 0x1)          # enable set-TxA-bias LUT
    sipi.set_rf_reg(t, path, 0x33, _FULL, 0x0)            # bank 0
    sipi.set_rf_reg(t, path, 0x3F, _FULL, rf3f)
    sipi.set_rf_reg(t, path, 0x33, 1 << 0, 0x1)           # bank 1
    sipi.set_rf_reg(t, path, 0x3F, _FULL, rf3f)
    sipi.set_rf_reg(t, path, 0x33, 1 << 1, 0x1)           # bank 3 (sets bit1; bit0 already 1)
    sipi.set_rf_reg(t, path, 0x3F, _FULL, rf3f)
    sipi.set_rf_reg(t, path, 0x33, 0x3, 0x3)              # bank 3 (idempotent)
    sipi.set_rf_reg(t, path, 0x3F, _FULL, rf3f)
    sipi.set_rf_reg(t, path, 0xEF, 1 << 10, 0x0)          # disable LUT


def get_pa_bias_offset(t, phy_map: bytes) -> None:
    """[SRC] phydm_get_pa_bias_offset / _8822b (halrf_kfree.c) — PG PA-bias trim to RF.

    Read PPG_PABIAS_2GA (efuse 0x3D5); if blank (0xFF) do nothing. Otherwise decode the signed 2GA
    (0x3D5) + 2GB (0x3D6) nibbles (bit0 = sign) and fold each path's offset into RF 0x3f (both paths,
    unconditionally). The three efuse reads each cost one WIFI bank-switch (0x35); the byte values
    come from the cached map."""
    from . import efuse
    pg = efuse.efuse_one_byte_read(t, phy_map, 0x3D5)
    if pg == 0xFF:
        return
    pg_a = efuse.efuse_one_byte_read(t, phy_map, 0x3D5) & 0xF
    bias_a = (pg_a >> 1) if (pg_a & 1) else -(pg_a >> 1)
    pg_b = efuse.efuse_one_byte_read(t, phy_map, 0x3D6) & 0xF
    bias_b = (pg_b >> 1) if (pg_b & 1) else -(pg_b >> 1)
    _set_pa_bias_to_rf(t, sipi.RF_PATH_A, bias_a)
    _set_pa_bias_to_rf(t, sipi.RF_PATH_B, bias_b)


def psd_init(t) -> None:
    """[SRC] phydm_psd_init -> phydm_psd_para_setting(dm, 1, 2, 3, 128, 0, 0, 7, 0) (phydm_psd.c) —
    the last wire-emitting odm_dm_init tail fn. 11AC caches psd_reg/report_reg (software) then writes
    the PSD-tool HW params into 0x910: i_q_setting=3, hw_avg_time=2, fft_smp_point_idx=0 (128 pt),
    ant_sel=0, psd_input=0. (The odm_dm_init fns between get_pa_bias and here — antdiv / soml / path_div
    / dynamic_tx / la / beamforming / primary_cca — are wire-silent on 8822b.)"""
    sipi.set_bb_reg(t, 0x0910, (1 << 11) | (1 << 10), 3)   # i_q_setting
    sipi.set_bb_reg(t, 0x0910, (1 << 13) | (1 << 12), 2)   # hw_avg_time
    sipi.set_bb_reg(t, 0x0910, (1 << 15) | (1 << 14), 0)   # fft_smp_point_idx (128 -> 0)
    sipi.set_bb_reg(t, 0x0910, (1 << 17) | (1 << 16), 0)   # ant_sel
    sipi.set_bb_reg(t, 0x0910, 1 << 23, 0)                 # psd_input


def _config_tx_path(t, tx_path: int, sel_1ss: int, sel_cck: int) -> None:
    """[SRC] phydm_config_tx_path_8822b + the CCK/OFDM TX-path helpers."""
    sipi.set_bb_reg(t, 0x093C, (1 << 19) | (1 << 18), 0x3)     # TX antenna by Nsts
    sipi.set_bb_reg(t, 0x080C, (1 << 29) | (1 << 28), 0x1)
    sipi.set_bb_reg(t, 0x080C, 1 << 30, 0x1)                   # CCK TX path by 0xa07[7]
    sipi.set_bb_reg(t, 0x080C, 0xFF, (tx_path << 4) | tx_path)  # TX path HW block enable
    # CCK TX path
    sipi.set_bb_reg(t, 0x0A04, 0xF0000000, {BB_PATH_A: 0x8, BB_PATH_B: 0x4}.get(sel_cck, 0xC))
    # OFDM TX logic map / path-en (tx_path_en == AB on this card)
    if tx_path == BB_PATH_A:
        sipi.set_bb_reg(t, 0x093C, 0xFFF00000, 0x001)
    elif tx_path == BB_PATH_B:
        sipi.set_bb_reg(t, 0x093C, 0xFFF00000, 0x002)
    else:                                                      # BB_PATH_AB, by 1ss selection
        m = {BB_PATH_A: 0x001, BB_PATH_B: 0x002}.get(sel_1ss, 0x043)
        sipi.set_bb_reg(t, 0x093C, 0xFFF00000, m)
        sipi.set_bb_reg(t, 0x0940, 0xFFF0, 0x043)
    if tx_path in (BB_PATH_A, BB_PATH_B):                      # Nsts=2 map (single-path only)
        sipi.set_bb_reg(t, 0x0940, 0xF0, 0x1)
        sipi.set_bb_reg(t, 0x0940, 0xFF00, 0x0)


def _config_rx_path(t, rx_path: int) -> None:
    """[SRC] phydm_config_rx_path_8822b: CCK MRC off, RX path enable, antenna-weight by Nrx."""
    sipi.set_bb_reg(t, 0x0A2C, 1 << 22, 0x0)                   # disable MRC for CCK CCA
    sipi.set_bb_reg(t, 0x0A2C, 1 << 18, 0x0)                   # disable MRC for CCK barker
    if rx_path & BB_PATH_A:
        sipi.set_bb_reg(t, 0x0A04, 0x0F000000, 0x0)
    elif rx_path & BB_PATH_B:
        sipi.set_bb_reg(t, 0x0A04, 0x0F000000, 0x5)
    sipi.set_bb_reg(t, 0x0808, 0xFF, (rx_path << 4) | rx_path)  # RX path enable
    ant_wgt = 0x0 if rx_path in (BB_PATH_A, BB_PATH_B) else 0x1
    sipi.set_bb_reg(t, 0x1904, 1 << 16, ant_wgt)              # antenna weighting
    sipi.set_bb_reg(t, 0x0800, 1 << 28, ant_wgt)             # htstf ant-wgt
    sipi.set_bb_reg(t, 0x0850, 1 << 23, ant_wgt)             # MRC mode (ZF eqz)


def config_trx_mode(t, central_ch: int = 0, tx_path: int = BB_PATH_AB,
                    rx_path: int = BB_PATH_AB, sel_1ss: int = BB_PATH_A,
                    rfe_type: int = 3, cut: int = 3) -> None:
    """[SRC] config_phydm_trx_mode_8822b — 2T2R path/mode config after the BB/RF tables.
    `rfe_type`/`cut` pick the FEM CCA table + eFEM 0x83c gate in the trailing ccapar (reference
    rfe 3 / D-cut -> iFEM-RFE, no 0x83c)."""
    sipi.set_bb_reg(t, 0x0C08, 0xFFFF, 0x3231 if (tx_path | rx_path) & BB_PATH_A else 0x1111)
    sipi.set_bb_reg(t, 0x0E08, 0xFFFF, 0x3231 if (tx_path | rx_path) & BB_PATH_B else 0x1111)
    _config_tx_path(t, tx_path, sel_1ss, sel_1ss)
    _config_rx_path(t, rx_path)
    # RF mode-table write + poll until RF_A 0x33 reads back 0x00001 (replay feeds the read).
    for _ in range(100):
        sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xEF, _FULL, 0x80000)
        sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0x33, _FULL, 0x00001)
        if sipi.read_rf_reg(t, sipi.RF_PATH_A, RF_0x33, _FULL) == 0x00001:
            break
    # Normal mode (not MP/antenna-test): the path-A 3-wire mode-table tail.
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xEF, _FULL, 0x80000)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0x33, _FULL, 0x00001)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0x3E, _FULL, 0x00034)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0x3F, _FULL, 0x4080C)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xEF, _FULL, 0x00000)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xEF, _FULL, 0x00000)
    chan._igi_toggle(t)                                       # let RF enter RX mode
    chan._ccapar_by_rfe(t, central_ch, True, rfe_type, cut,   # central_ch 0 -> col 1 (2.4G/2R)
                        ant_2r=(rx_path == BB_PATH_AB))
    # phydm_rfe_8822b(central_ch): channel 0 -> returns without writing.
