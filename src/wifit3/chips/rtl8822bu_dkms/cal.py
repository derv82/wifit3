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


def init_cck_setting(t) -> None:
    """[SRC] phydm_init_cck_setting + phydm_config_cck_rx_antenna_init (2SS) — CCK RX setup.

    cck_new_agc_chk reads 0xA9C[17]; is_cck_high_power reads CCK_RPT_FORMAT (0x804); both cache
    software flags (the replay feeds them). cck_lna_bit_num_chk is a no-op on 8822b."""
    sipi.get_bb_reg(t, 0x0A9C, 1 << 17)              # cck_new_agc flag
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


def common_info_self_init(t, rfe_type: int, channel: int = 1) -> None:
    """[SRC] phydm_common_info_self_init (phydm.c:238) — DM self-init after phydm_rfe_init.

    Only three steps touch the wire: phydm_init_cck_setting (CCK RX setup), the BB_RX_PATH read
    that caches rf_path_rx_enable (ODM_REG_BB_RX_PATH_11AC 0x808, software-only), and
    phydm_init_soft_ml_setting (SoML RxHP seed). The CE-gated debug-setting block and
    phydm_trx_antenna_setting_init (a no-op on 8822b — only 8192F/E/97F + 8812/8814A read regs
    there) emit nothing. `channel` is the cold default (2.4 GHz, ≤14)."""
    init_cck_setting(t)                                # phydm_init_cck_setting
    sipi.get_bb_reg(t, 0x0808, 0xF)                    # rf_path_rx_enable (BB_RX_PATH, cached)
    init_soft_ml_setting(t, channel, rfe_type)         # phydm_init_soft_ml_setting


def dig_init(t) -> None:
    """[SRC] phydm_dig_init (phydm_dig.c:1000) — DIG (RX IGI) seed.

    Mostly software field-setup; the only wire reads are the current IGI (phydm_get_igi path-A,
    0xC50[6:0]) into cur_ig_value, and the 8822b big-jump steps from 0x8C8[15:0] (the replay feeds
    both)."""
    sipi.get_bb_reg(t, 0x0C50, 0x7F)              # phydm_get_igi(path A) -> cur_ig_value
    sipi.get_bb_reg(t, 0x08C8, 0xFFFF)            # big_jump_step1/2/3 (RTL8822B)


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
                    rx_path: int = BB_PATH_AB, sel_1ss: int = BB_PATH_A) -> None:
    """[SRC] config_phydm_trx_mode_8822b — 2T2R path/mode config after the BB/RF tables."""
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
    chan._ccapar_by_rfe(t, central_ch, bw20=True)             # central_ch 0 -> col 1 (2.4G/2R)
    # phydm_rfe_8822b(central_ch): channel 0 -> returns without writing.
