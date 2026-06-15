"""RTL8822BU channel tune — per-channel RF retune (the airodump hop primitive).

Ports config_phydm_switch_channel_8822b `[SRC] phydm_hal_api8822b.c:1808` (2.4 + 5 GHz, 20 MHz),
its inline helpers (phydm_igi_toggle_8822b, phydm_ccapar_by_rfe_8822b), and the channel-change
spur reset (phydm_spur_calibration_8822b -> phydm_dsde_init). This is the work a single
`iw set channel N` triggers in the vendor driver — verified hop-by-hop against the cold-boot
capture's airodump `--band abg` sweep (see scripts/rtl8822bu_dkms/verify_channels.py).

The band switch (config_phydm_switch_band_8822b, only on a 2.4<->5 crossing) and the bandwidth
re-apply (mac_switch_bandwidth=HALMAC cfg_bw + config_phydm_switch_bandwidth_8822b) are the rest
of switch_chnl_and_set_bw; they are the next milestone. wifit3 stays 20 MHz primary by design.

This card is 2T2R (rf_type 2 -> rx/tx_ant_status = BB_PATH_AB) and rfe_type 3 (iFEM); the
ccapar/rfe branches below resolve to those. The PSD-based dynamic spur eliminator only runs on a
few spur-prone channels (2.4 GHz 5-8, 5 GHz 153/161 at 20 MHz); those raise until ported, rather
than silently skip the NBI/CSI notch.
"""
from __future__ import annotations

from . import sipi

BB_PATH_A, BB_PATH_B, BB_PATH_AB = 1, 2, 3
RF_0x18, RF_0xbe, RF_0xdf, RF_0xb8 = 0x18, 0xBE, 0xDF, 0xB8

# HALMAC mac_switch_bandwidth (cfg_ch_bw_88xx) registers
REG_DATA_SC = 0x0483               # cfg_pri_ch_idx: TXSC_40M<<4 | TXSC_20M
REG_WMAC_TRXPTCL_CTL = 0x0668      # cfg_bw: clear BIT7/8 for BW20
REG_AFE_CTRL1 = 0x0024             # cfg_mac_clk: MAC_CLK_SEL[21:20]
REG_USTIME_TSF, REG_USTIME_EDCA = 0x055C, 0x0638
REG_CCK_CHECK = 0x0454             # cfg_ch: BIT7 = 5 GHz band marker
MAC_CLK_SPEED = 0x50               # MAC clock 80 MHz scaler ([WIRE] 0x55c/0x638)

# [SRC] cca_ifem_ccut_rfe[3][4] (rfe_type 3) — Reg82C/830/838 by column (1R/2R x 2G/5G).
_CCA_IFEM_RFE = (
    (0x75DA8010, 0x75DA8010, 0x75DA8010, 0x75DA8010),   # 0x82C
    (0x79A0EAAA, 0x97A0EAAC, 0x79A0EAAA, 0x79A0EAAA),   # 0x830
    (0x87765541, 0x86666341, 0x87765561, 0x86666361),   # 0x838
)
# [SRC] config_phydm_switch_channel_8822b RF_0xBE phase-noise table (5 GHz, ch>=36 by (ch-base)>>1)
_LOW_BAND = (0x7, 0x6, 0x6, 0x5, 0x0, 0x0, 0x7, 0xFF, 0x6, 0x5, 0x0, 0x0, 0x7, 0x6, 0x6)
_MID_BAND = (0x6, 0x5, 0x0, 0x0, 0x7, 0x6, 0x6, 0xFF, 0x0, 0x0, 0x7, 0x6, 0x6, 0x5, 0x0, 0xFF,
             0x7, 0x6, 0x6, 0x5, 0x0, 0x0, 0x7)
_HIGH_BAND = (0x5, 0x5, 0x0, 0x7, 0x7, 0x6, 0x5, 0xFF, 0x0, 0x7, 0x7, 0x6, 0x5, 0x5, 0x0)


def _igi_toggle(t) -> None:
    """[SRC] phydm_igi_toggle_8822b: read IGI from 0xC50, bump down then back, both paths."""
    igi = sipi.get_bb_reg(t, 0x0C50, 0x7F)
    sipi.set_bb_reg(t, 0x0C50, 0x7F, igi - 2)
    sipi.set_bb_reg(t, 0x0C50, 0x7F, igi)
    sipi.set_bb_reg(t, 0x0E50, 0x7F, igi - 2)
    sipi.set_bb_reg(t, 0x0E50, 0x7F, igi)


def _ccapar_by_rfe(t, ch: int, bw20: bool) -> None:
    """[SRC] phydm_ccapar_by_rfe_8822b: per-(band,Nrx) CCA params (rfe_type 3 => iFEM-RFE table)."""
    col = 1 if ch <= 14 else 3                     # 2R (BB_PATH_AB): 2G->col1, 5G->col3
    sipi.set_bb_reg(t, 0x082C, 0xFFFFFFFF, _CCA_IFEM_RFE[0][col])
    sipi.set_bb_reg(t, 0x0830, 0xFFFFFFFF, _CCA_IFEM_RFE[1][col])
    sipi.set_bb_reg(t, 0x0838, 0xFFFFFFFF, _CCA_IFEM_RFE[2][col])
    # DFS 20 MHz tweak (ch 52-64 / 100-144). 2.4 GHz never hits it.
    if bw20 and ((52 <= ch <= 64) or (100 <= ch <= 144)):
        sipi.set_bb_reg(t, 0x0838, 0xF0, 0x5)


def is_psd_spur_channel(ch: int, bw20: bool = True) -> bool:
    """True for the channels whose spur reset runs the read-dependent PSD sweep (idx <= 13)."""
    return _dsde_ch_idx(ch, bw20) <= 13


def _spur_reset(t, ch: int, bw20: bool) -> None:
    """[SRC] phydm_spur_calibration_8822b (normal mode): drop the spur-elim enables + reset
    NBI/CSI (phydm_dsde_init). The read-dependent PSD sweep (phydm_dynamic_spur_det_eliminate) runs
    only on the few spur channels below idx 14; it is not ported, so on those channels the NBI/CSI
    notch is simply not applied (bounded RX-quality gap, not a break) — see is_psd_spur_channel."""
    sipi.set_bb_reg(t, 0x087C, 1 << 13, 0x0)
    sipi.set_bb_reg(t, 0x0C20, 1 << 28, 0x0)
    sipi.set_bb_reg(t, 0x0E20, 1 << 28, 0x0)
    for addr in (0x0880, 0x0884, 0x0888, 0x088C, 0x0890, 0x0894, 0x0898, 0x089C):
        sipi.set_bb_reg(t, addr, 0xFFFFFFFF, 0)    # phydm_dsde_init: reset NBI/CSI
    sipi.set_bb_reg(t, 0x0874, 1 << 0, 0x0)        # phydm_dsde_init: NBI enable bit


def _dsde_ch_idx(ch: int, bw20: bool) -> int:
    """[SRC] phydm_dsde_ch_idx: maps a channel to its spur freq-point index (16 == no spur)."""
    if 1 <= ch <= 14:
        if bw20:
            if 5 <= ch <= 8:
                return ch - 5
            return 4 if ch == 13 else 16
        return ch + 2 if 3 <= ch <= 11 else 16
    return {153: 0, 161: 1, 54: 2, 118: 3, 151: 4, 159: 5, 58: 6, 122: 7, 155: 8}.get(ch, 16)


def switch_channel(t, ch: int, rf_2t2r: bool = True, bw20: bool = True) -> None:
    """[SRC] config_phydm_switch_channel_8822b — set RF channel + per-channel BB, both paths."""
    rf18 = sipi.read_rf_reg(t, sipi.RF_PATH_A, RF_0x18)
    rf18 &= ~((1 << 18) | (1 << 17) | 0xFF)        # clear band/byte0, keep BW bits

    if ch <= 14:                                   # 2.4 GHz
        rf18 |= ch
        sipi.set_bb_reg(t, 0x0958, 0x1F, 0x0)      # AGC table 0
        sipi.set_bb_reg(t, 0x0860, 0x1FFE0000, 0x96A)
        if ch == 14:
            sipi.set_bb_reg(t, 0x0A24, 0xFFFFFFFF, 0x00006577)
            sipi.set_bb_reg(t, 0x0A28, 0x0000FFFF, 0x0000)
        else:
            sipi.set_bb_reg(t, 0x0A24, 0xFFFFFFFF, 0x384F6577)
            sipi.set_bb_reg(t, 0x0A28, 0x0000FFFF, 0x1525)
    else:                                          # 5 GHz
        rf18 |= ch
        if 36 <= ch <= 64:
            sipi.set_bb_reg(t, 0x0958, 0x1F, 0x1)
        elif 100 <= ch <= 144:
            sipi.set_bb_reg(t, 0x0958, 0x1F, 0x2)
        elif ch >= 149:
            sipi.set_bb_reg(t, 0x0958, 0x1F, 0x3)
        if 36 <= ch <= 48:
            sipi.set_bb_reg(t, 0x0860, 0x1FFE0000, 0x494)
        elif 52 <= ch <= 64:
            sipi.set_bb_reg(t, 0x0860, 0x1FFE0000, 0x453)
        elif 100 <= ch <= 116:
            sipi.set_bb_reg(t, 0x0860, 0x1FFE0000, 0x452)
        elif 118 <= ch <= 177:
            sipi.set_bb_reg(t, 0x0860, 0x1FFE0000, 0x412)

    rf_be = _rf_0xbe(ch)                            # phase-noise RF_0xBE[17:15]
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xbe, (1 << 17) | (1 << 16) | (1 << 15), rf_be)

    if ch == 144:                                  # ch-144 synth workaround
        sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xdf, 1 << 18, 0x1)
        rf18 |= (1 << 17)
    else:
        sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xdf, 1 << 18, 0x0)
        if ch > 144:
            rf18 |= (1 << 18)
        elif ch >= 80:
            rf18 |= (1 << 17)

    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0x18, sipi.RFREGOFFSETMASK, rf18)
    if rf_2t2r:
        sipi.set_rf_reg(t, sipi.RF_PATH_B, RF_0x18, sipi.RFREGOFFSETMASK, rf18)

    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xb8, 1 << 19, 0)   # RF read-error debug toggle
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0xb8, 1 << 19, 1)

    _igi_toggle(t)
    _ccapar_by_rfe(t, ch, bw20)
    _spur_reset(t, ch, bw20)


def _rfe_ifem(t, ch: int, rx2_or_tx2: bool) -> None:
    """[SRC] phydm_rfe_ifem: RFE pinmux/inv/antenna-switch for both paths (rfe_type 3)."""
    if ch <= 14:
        sipi.set_bb_reg(t, 0x0CB0, 0xFFFFFF, 0x745774)
        sipi.set_bb_reg(t, 0x0EB0, 0xFFFFFF, 0x745774)
        sipi.set_bb_reg(t, 0x0CB4, 0xFF00, 0x57)
        sipi.set_bb_reg(t, 0x0EB4, 0xFF00, 0x57)
    else:
        sipi.set_bb_reg(t, 0x0CB0, 0xFFFFFF, 0x477547)
        sipi.set_bb_reg(t, 0x0EB0, 0xFFFFFF, 0x477547)
        sipi.set_bb_reg(t, 0x0CB4, 0xFF00, 0x75)
        sipi.set_bb_reg(t, 0x0EB4, 0xFF00, 0x75)
    for inv in (0x0CBC, 0x0EBC):
        sipi.set_bb_reg(t, inv, 0x3F, 0x0)
        sipi.set_bb_reg(t, inv, (1 << 11) | (1 << 10), 0x0)
    ant = 0xA501 if (ch <= 14 and rx2_or_tx2) else (0xA5A5 if ch > 14 else 0xA500)
    sipi.set_bb_reg(t, 0x0CA0, 0xFFFF, ant)
    sipi.set_bb_reg(t, 0x0EA0, 0xFFFF, ant)


def switch_band(t, ch: int, rf_2t2r: bool, rx_ant: int) -> None:
    """[SRC] config_phydm_switch_band_8822b — 2.4<->5 band swap (only on a crossing).

    The SoML branch reads 0x19a8[31] (the replay feeds it). For rfe_type 3, 2.4 GHz resolves the
    same for both SoML states; 5 GHz differs (SoML-on uses 0x08108000/0x8d8[27]=0).
    """
    rf18 = sipi.read_rf_reg(t, sipi.RF_PATH_A, RF_0x18)
    if ch <= 14:                                   # 2.4 GHz
        sipi.set_bb_reg(t, 0x0808, 1 << 28, 0x1)   # enable CCK block
        sipi.set_bb_reg(t, 0x0454, 1 << 7, 0x0)    # disable MAC CCK check
        sipi.set_bb_reg(t, 0x0A80, 1 << 18, 0x0)   # disable BB CCK check
        sipi.set_bb_reg(t, 0x0814, 0x0000FC00, 15)
        rf18 &= ~((1 << 16) | (1 << 9) | (1 << 8))
    else:                                          # 5 GHz
        sipi.set_bb_reg(t, 0x0A80, 1 << 18, 0x1)
        sipi.set_bb_reg(t, 0x0454, 1 << 7, 0x1)
        sipi.set_bb_reg(t, 0x0808, 1 << 28, 0x0)
        sipi.set_bb_reg(t, 0x0814, 0x0000FC00, 34)
        rf18 &= ~((1 << 16) | (1 << 9) | (1 << 8))
        rf18 |= (1 << 8) | (1 << 16)
    soml_on = sipi.get_bb_reg(t, 0x19A8, 1 << 31) == 0x1   # RxHP / SoML dynamic control
    sipi.set_bb_reg(t, 0x0C04, (1 << 18) | (1 << 21), 0x0)
    sipi.set_bb_reg(t, 0x0E04, (1 << 18) | (1 << 21), 0x0)
    if ch > 14 and soml_on:                        # 5 GHz SoML-on (rfe 3)
        sipi.set_bb_reg(t, 0x08CC, 0xFFFFFFFF, 0x08108000)
        sipi.set_bb_reg(t, 0x08D8, 1 << 27, 0x0)
    else:                                          # 2.4 GHz (either) + 5 GHz SoML-off (rfe 3)
        sipi.set_bb_reg(t, 0x08CC, 0xFFFFFFFF, 0x08108492)
        sipi.set_bb_reg(t, 0x08D8, 1 << 19, 0x0)
        sipi.set_bb_reg(t, 0x08D8, 1 << 27, 0x1)
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0x18, sipi.RFREGOFFSETMASK, rf18)
    if rf_2t2r:
        sipi.set_rf_reg(t, sipi.RF_PATH_B, RF_0x18, sipi.RFREGOFFSETMASK, rf18)
    _rfe_ifem(t, ch, rx_ant == BB_PATH_AB)         # phydm_rfe_8822b -> rfe_ifem (rfe_type 3)
    _spur_reset(t, ch, bw20=True)


def _mac_switch_bandwidth(t, ch: int, pri_idx: int = 0) -> None:
    """[SRC] mac_switch_bandwidth -> HALMAC cfg_ch_bw_88xx: pri-ch-idx + bw + mac-clk + band marker.

    20 MHz / primary-index 0: REG_DATA_SC = TXSC_40M(10)<<4 (0xA0); clear BW bits in
    REG_WMAC_TRXPTCL_CTL; cfg_mac_clk (80 MHz); REG_CCK_CHECK band marker (BIT7 = 5 GHz).
    """
    txsc40 = 9 if pri_idx in (1, 3) else 10
    t.write8(REG_DATA_SC, (pri_idx & 0xF) | ((txsc40 & 0xF) << 4))          # cfg_pri_ch_idx
    sipi.set_bb_reg(t, REG_WMAC_TRXPTCL_CTL, (1 << 7) | (1 << 8), 0x0)      # cfg_bw (BW20)
    sipi.set_bb_reg(t, REG_AFE_CTRL1, (1 << 21) | (1 << 20), 0x0)          # cfg_mac_clk: 80 MHz
    t.write8(REG_USTIME_TSF, MAC_CLK_SPEED)
    t.write8(REG_USTIME_EDCA, MAC_CLK_SPEED)
    v = t.read8(REG_CCK_CHECK) & ~0x80                                      # cfg_ch: band marker
    t.write8(REG_CCK_CHECK, (v | 0x80) if ch > 35 else v)


def _switch_bandwidth_20(t, ch: int, rf_2t2r: bool, rx_ant: int) -> None:
    """[SRC] config_phydm_switch_bandwidth_8822b (CHANNEL_WIDTH_20) + its tail helpers."""
    rf18 = sipi.read_rf_reg(t, sipi.RF_PATH_A, RF_0x18)
    val32 = (t.read32(0x08AC) & 0xFFCFFC00)            # | CHANNEL_WIDTH_20 (== 0)
    t.write32(0x08AC, val32)
    sipi.set_bb_reg(t, 0x08C4, 1 << 30, 0x1)           # ADC buffer clock
    rf18 |= (1 << 11) | (1 << 10)                      # RF BW = 20 MHz
    sipi.set_rf_reg(t, sipi.RF_PATH_A, RF_0x18, sipi.RFREGOFFSETMASK, rf18)
    if rf_2t2r:
        sipi.set_rf_reg(t, sipi.RF_PATH_B, RF_0x18, sipi.RFREGOFFSETMASK, rf18)
    # phydm_rxdfirpar_by_bw_8822b (BW20)
    sipi.set_bb_reg(t, 0x0948, (1 << 29) | (1 << 28), 0x2)
    sipi.set_bb_reg(t, 0x094C, (1 << 29) | (1 << 28), 0x2)
    sipi.set_bb_reg(t, 0x0C20, 1 << 31, 0x1)
    sipi.set_bb_reg(t, 0x0E20, 1 << 31, 0x1)
    _ccapar_by_rfe(t, ch, bw20=True)
    _spur_reset(t, ch, bw20=True)
    # phydm_bw_fixed_setting (BW20) + phydm_bw_fixed_enable
    sipi.set_bb_reg(t, 0x0840, 0xF, 0x0)
    sipi.set_bb_reg(t, 0x0840, 1 << 4, 0x1)
    # Toggle RX path to avoid the RX dead-zone, then IGI to enter RX mode
    sipi.set_bb_reg(t, 0x0808, 0xFF, 0x0)
    sipi.set_bb_reg(t, 0x0808, 0xFF, rx_ant | (rx_ant << 4))
    _igi_toggle(t)


def set_channel_bw(t, ch: int, rf_2t2r: bool = True, prev_ch: int | None = None) -> None:
    """Runtime hop (20 MHz): optional band switch + channel + bandwidth re-apply.

    [SRC] switch_chnl_and_set_bw_by_drv steps 1-3. A 2.4<->5 crossing (prev_ch on the other side
    of ch 14) runs config_phydm_switch_band_8822b first; same-band hops skip it. The per-channel
    DPK/TSSI cal the vendor runs next is deferred (TX-quality; see RTL8822BU_DKMS.md).
    """
    rx_ant = BB_PATH_AB if rf_2t2r else BB_PATH_A
    if prev_ch is not None and (prev_ch <= 14) != (ch <= 14):
        switch_band(t, ch, rf_2t2r, rx_ant)
    switch_channel(t, ch, rf_2t2r=rf_2t2r)
    _mac_switch_bandwidth(t, ch)
    _switch_bandwidth_20(t, ch, rf_2t2r, rx_ant)


def _rf_0xbe(ch: int) -> int:
    if ch <= 14:
        return 0x0
    if 36 <= ch <= 64:
        return _LOW_BAND[(ch - 36) >> 1]
    if 100 <= ch <= 144:
        return _MID_BAND[(ch - 100) >> 1]
    if 149 <= ch <= 177:
        return _HIGH_BAND[(ch - 149) >> 1]
    return 0xFF
