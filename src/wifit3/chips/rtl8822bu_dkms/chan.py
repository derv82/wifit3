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
ccapar/rfe branches below resolve to those. The PSD-based dynamic spur eliminator
(phydm_dynamic_spur_det_eliminate) runs the full read-dependent PSD sweep + NBI/CSI notch on the
spur-prone channels (2.4 GHz 5-8/13, 5 GHz 153/161 at 20 MHz); on the replay the 0xF44 PSD reads
are fed back, so the threshold branch reproduces the capture's notch byte-for-byte.
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

# [SRC] phydm_dynamic_spur_det_eliminate (phydm_hal_api8822b.c:1342) — PSD probe freq points by
# spur idx (the |+/-1| neighbours are computed at use). Path-B reuses the 2G/5G point | BIT(16).
_FREQ_2G = (0xFC67, 0xFC27, 0xFFE6, 0xFFA6, 0xFC67, 0xFCE7, 0xFCA7, 0xFC67, 0xFC27, 0xFFE6,
            0xFFA6, 0xFF66, 0xFF26, 0xFCE7)
_FREQ_5G = (0xFFC0, 0xFFC0, 0xFC81, 0xFC81, 0xFC41, 0xFC40, 0xFF80, 0xFF80, 0xFF40, 0xFD42)
_PSD_NBI_TH = 0x8D                 # PSD >= this on either path => apply the NBI + CSI notch
# [SRC] phydm_set_nbi_reg nbi_128[] (phydm_api.c:992) — FFT-128 tone boundaries (tone_idx x 10);
# reg_idx = 1 + index of the first boundary the tone is below. 8822b BW20/40 uses FFT-128.
_NBI_128 = (25, 55, 85, 115, 135, 155, 185, 205, 225, 245, 265, 285, 305, 335, 355,
            375, 395, 415, 435, 455, 485, 505, 525, 555, 585, 615, 635)


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


def _spur_reset(t, ch: int, bw20: bool, rf_2t2r: bool = True, rfe_type: int = 3) -> None:
    """[SRC] phydm_spur_calibration_8822b (normal mode, not scan-in-process): drop the spur-elim
    enables, then run phydm_dynamic_spur_det_eliminate (NBI/CSI reset + the PSD spur sweep)."""
    sipi.set_bb_reg(t, 0x087C, 1 << 13, 0x0)
    sipi.set_bb_reg(t, 0x0C20, 1 << 28, 0x0)
    sipi.set_bb_reg(t, 0x0E20, 1 << 28, 0x0)
    _dynamic_spur_det_eliminate(t, ch, bw20, rf_2t2r, rfe_type)


def _dsde_init(t) -> None:
    """[SRC] phydm_dsde_init: clear the NBI/CSI notch mask (0x880-0x89C) + the CSI-mask enable
    (0x874[0]) on every channel/BW/band change."""
    for addr in (0x0880, 0x0884, 0x0888, 0x088C, 0x0890, 0x0894, 0x0898, 0x089C):
        sipi.set_bb_reg(t, addr, 0xFFFFFFFF, 0)
    sipi.set_bb_reg(t, 0x0874, 1 << 0, 0x0)


def _dynamic_spur_det_eliminate(t, ch: int, bw20: bool, rf_2t2r: bool, rfe_type: int) -> None:
    """[SRC] phydm_dynamic_spur_det_eliminate: reset the notch, then on a spur channel (idx <= 13)
    sweep the PSD at the spur freq point (+/-1 neighbours, both paths, PSD_SMP_NUM x PSD_VAL_NUM)
    and, if the peak crosses the threshold on either path, apply the NBI + CSI notch.

    The PSD is read from 0xF44; on the replay every read is fed back, so the threshold branch (and
    thus whether the notch fires) reproduces the capture exactly. On hardware it measures live spur."""
    _dsde_init(t)
    idx = _dsde_ch_idx(ch, bw20)
    if idx > 13:
        return                                       # no spur freq point on this channel
    rx_ant = BB_PATH_AB if rf_2t2r else BB_PATH_A
    base = _FREQ_2G[idx] if ch <= 14 else _FREQ_5G[idx]
    peak_a = peak_b = 0
    for k in range(3):                               # PSD_SMP_NUM: f-1, f, f+1
        f_pt = (base + (k - 1)) & 0xFFFF
        f_pt_b = f_pt | (1 << 16)
        smp_a = smp_b = 0
        for _ in range(3):                           # PSD_VAL_NUM samples per point
            sipi.set_bb_reg(t, 0x0C00, 0xFF, 0x4)    # disable 3-wire, both paths
            sipi.set_bb_reg(t, 0x0E00, 0xFF, 0x4)
            saved = sipi.get_bb_reg(t, 0x0910, 0xF000)
            if rx_ant & BB_PATH_A:
                sipi.set_bb_reg(t, 0x0808, 0xFF, (BB_PATH_A << 4) | BB_PATH_A)
                sipi.set_bb_reg(t, 0x0910, 0xFFFFFFFF, (1 << 22) | f_pt)   # start PSD @ f_pt
                smp_a = max(smp_a, sipi.get_bb_reg(t, 0x0F44, 0xFFFF))
                sipi.set_bb_reg(t, 0x0910, 1 << 22, 0x0)                   # stop PSD
            if rx_ant & BB_PATH_B:
                sipi.set_bb_reg(t, 0x0808, 0xFF, (BB_PATH_B << 4) | BB_PATH_B)
                sipi.set_bb_reg(t, 0x0910, 0xFFFFFFFF, (1 << 22) | f_pt_b)
                smp_b = max(smp_b, sipi.get_bb_reg(t, 0x0F44, 0xFFFF))
                sipi.set_bb_reg(t, 0x0910, 1 << 22, 0x0)
            sipi.set_bb_reg(t, 0x0C00, 0xFF, 0x7)    # re-enable 3-wire
            sipi.set_bb_reg(t, 0x0E00, 0xFF, 0x7)
            sipi.set_bb_reg(t, 0x0910, 0xF000, saved)
            sipi.set_bb_reg(t, 0x0808, 0xFF, (rx_ant << 4) | rx_ant)
            _igi_toggle(t)                           # let RF re-enter RX mode
        peak_a, peak_b = max(peak_a, smp_a), max(peak_b, smp_b)
    if peak_a >= _PSD_NBI_TH or peak_b >= _PSD_NBI_TH:
        _dsde_nbi(t, ch, bw20, rf_2t2r, rfe_type)
        _dsde_csi(t, ch, bw20)


def _spur_f_intf(ch: int) -> int | None:
    """[SRC] phydm_dsde_nbi/phydm_dsde_csi (20 MHz) — the interference freq (MHz) per spur channel."""
    if ch == 153:
        return 5760
    if ch == 161:
        return 5800
    if ch == 13:
        return 2480
    return 2440 if 5 <= ch <= 8 else None


def _find_fc(ch: int) -> int | None:
    """[SRC] phydm_find_fc (20 MHz primary) — channel centre freq (MHz)."""
    if 1 <= ch <= 14:
        return 2412 + (ch - 1) * 5
    if 36 <= ch <= 177:
        return 5180 + (ch - 36) * 5
    return None


def _find_intf_distance(fc: int, f_intf: int, bw: int = 20) -> int | None:
    """[SRC] phydm_find_intf_distance — tone index (x10) for an interferer inside the band, else None."""
    if fc - bw // 2 <= f_intf <= fc + bw // 2:
        return abs(fc - f_intf) << 5                 # 10 * (dist / 0.3125)
    return None


def _set_nbi_reg(t, tone_idx: int) -> None:
    """[SRC] phydm_set_nbi_reg (8822b FFT-128): map the tone index to reg_idx, write 0x87C[19:14]."""
    reg_idx = 0
    for i, bound in enumerate(_NBI_128):
        if tone_idx < bound:
            reg_idx = i + 1
            break
    sipi.set_bb_reg(t, 0x087C, 0xFC000, reg_idx)


def _nbi_enable(t, enable: bool, rf_2t2r: bool) -> None:
    """[SRC] phydm_nbi_enable (8822b): NBI on/off at 0x87C[13] + 0xC20[28] (+ 0xE20[28] for 2T)."""
    val = 1 if enable else 0
    sipi.set_bb_reg(t, 0x087C, 1 << 13, val)
    sipi.set_bb_reg(t, 0x0C20, 1 << 28, val)
    if rf_2t2r:
        sipi.set_bb_reg(t, 0x0E20, 1 << 28, val)


def _dsde_nbi(t, ch: int, bw20: bool, rf_2t2r: bool, rfe_type: int) -> None:
    """[SRC] phydm_dsde_nbi: CCA-param tweak for NBI, then phydm_nbi_setting at the spur freq."""
    sipi.set_bb_reg(t, 0x082C, 0xFF000, 0x86 if rfe_type in (15, 16) else 0x97)
    if rfe_type in (12, 19):
        if bw20 and 5 <= ch <= 7:
            sipi.set_bb_reg(t, 0x082C, 0xF000, 0x3)
    else:
        sipi.set_bb_reg(t, 0x082C, 0xF000, 0x7)
    f_intf = _spur_f_intf(ch) if bw20 else None
    fc = _find_fc(ch)
    tone = _find_intf_distance(fc, f_intf) if (f_intf is not None and fc is not None) else None
    if tone is not None:                             # phydm_nbi_setting: SUCCESS path
        _set_nbi_reg(t, tone)
        _nbi_enable(t, True, rf_2t2r)
    else:
        _nbi_enable(t, False, rf_2t2r)


def _set_csi_mask(t, tone_idx: int, positive: bool) -> None:
    """[SRC] phydm_set_csi_mask (8822b, 128-tone): set one mask bit at 0x880 (pos) / 0x890 (neg)."""
    tone = tone_idx + 10 if (tone_idx % 10) >= 5 else tone_idx
    tone //= 10
    if positive:
        tone = min(tone, 127)
        reg, bit = 0x0880 + (tone >> 3), tone & 0x7
    else:
        tone = 128 - min(tone, 128)
        reg, bit = 0x0890 + (tone >> 3), tone & 0x7
    t.write8(reg, t.read8(reg) | (1 << bit))


def _dsde_csi(t, ch: int, bw20: bool) -> None:
    """[SRC] phydm_dsde_csi -> phydm_csi_mask_setting: mask the spur tone, then enable at 0x874[0]."""
    f_intf = _spur_f_intf(ch) if bw20 else None
    fc = _find_fc(ch)
    tone = _find_intf_distance(fc, f_intf) if (f_intf is not None and fc is not None) else None
    if tone is not None:
        _set_csi_mask(t, tone, f_intf >= fc)
        sipi.set_bb_reg(t, 0x0874, 1 << 0, 0x1)      # phydm_csi_mask_enable
    else:
        sipi.set_bb_reg(t, 0x0874, 1 << 0, 0x0)


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
    _spur_reset(t, ch, bw20, rf_2t2r)


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
    _spur_reset(t, ch, True, rf_2t2r)


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
    _spur_reset(t, ch, True, rf_2t2r)
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
