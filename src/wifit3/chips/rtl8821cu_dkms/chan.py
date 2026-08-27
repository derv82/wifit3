"""RTL8821CU channel tune — the phydm band/channel/bandwidth set the airmon channel hop drives.

[SRC] core/rtw_wlan_util.c:489 set_channel_bwmode -> rtw_hal_set_chnl_bw -> rtl8821c_set_channel_bw
([SRC] rtl8821c_phy.c:919) -> rtl8821c_switch_chnl_and_set_bw ([SRC] :740). For a 20 MHz channel the
center channel equals the primary channel. The first set (from ``init_hw_mlme_ext``) is channel 1,
2.4 GHz; the same path replays per airodump hop. Values are computed from the read-back RF/BB state,
not transcribed.
"""
from __future__ import annotations

from . import btc, dm, efuse, txpower
from .bb import set_bb_reg
from .rf import read_rf, write_rf, write_rf_masked

_MASKDWORD = 0xFFFFFFFF
_KFREE_GAIN_BMASK = 0x7C000        # RF 0x55/0x65 [18:14] kfree gain field
_BAND_2_4G, _BAND_5G = 0, 1       # [SRC] include/rtw_rf.h:97
_SWITCH_TO_BTG, _SWITCH_TO_WLG, _SWITCH_TO_WLA = 0, 1, 2   # [SRC] phydm_hal_api8821c.h rf_set enum
_ODM_CUT_A = 0                    # [SRC] phydm_pre_define.h:753 — A-cut LCK fix branch (RF 0xb8)


def _need_switch_band(t, channel: int) -> bool:
    """need_switch_band [SRC] rtl8821c_phy.c:477 — TRUE (and latches ``t.current_band``) when the
    channel's band differs from the last tune's. The first set always switches (band starts
    invalid); a same-band airodump hop skips the coex-notify + phydm band-switch + bb-swing
    sub-step (`phy_switch_wireless_band_8821c` runs it only inside this gate)."""
    band_to_sw = _BAND_5G if channel > 14 else _BAND_2_4G
    if band_to_sw != t.current_band:
        t.current_band = band_to_sw
        return True
    return False


def _switch_rf_set(t, rf_set: int) -> None:
    """config_phydm_switch_rf_set_8821c [SRC] phydm_hal_api8821c.c:1240 — route the RF front-end to
    the BTG/WLG/WLA set: 0x1080[16]=1, 0x00[26]=1, then merge the 0xcb8 select bits (and, for the
    2.4 GHz BTG/WLG sets, the 0xa84/0xa80 gains). The 5G WLA set only flips the 0xcb8 bits.
    ``mp_mode`` is off so the AGC-diff retune is skipped."""
    set_bb_reg(t, 0x1080, 1 << 16, 0x1)
    set_bb_reg(t, 0x0000, 1 << 26, 0x1)
    bb = t.read32(0x0CB8)
    if rf_set == _SWITCH_TO_BTG:
        bb = (bb | (1 << 16)) & ~((1 << 18) | (1 << 20) | (1 << 21) | (1 << 22) | (1 << 23))
        set_bb_reg(t, 0x0A84, 0x00FF0000, 0xE)
        set_bb_reg(t, 0x0A80, 0x0000FFFF, 0xFC84)
    elif rf_set == _SWITCH_TO_WLG:
        bb = (bb | (1 << 20) | (1 << 21) | (1 << 22)) & ~((1 << 16) | (1 << 18) | (1 << 23))
        set_bb_reg(t, 0x0A84, 0x00FF0000, 0x12)
        set_bb_reg(t, 0x0A80, 0x0000FFFF, 0x7532)
    else:                                                   # SWITCH_TO_WLA (5G) [SRC] :1308
        bb = (bb | (1 << 20) | (1 << 22) | (1 << 23)) & ~((1 << 16) | (1 << 18) | (1 << 21))
    set_bb_reg(t, 0x0CB8, _MASKDWORD, bb)


def _switch_band(t, info, central_ch: int) -> None:
    """config_phydm_switch_band_8821c [SRC] phydm_hal_api8821c.c:707 (2.4 GHz arm). Enable the CCK
    block, clear the MAC/BB CCK checks, set the CCA mask, route the RF set, then write the RF band/
    channel word (RF 0x18) with TRX stopped. ``phydm_rfe_8821c`` after it is `#if 0` (silent)."""
    rf18 = read_rf(t, 0x18)
    set_bb_reg(t, 0x0808, 1 << 28, 0x1)                     # enable CCK block
    set_bb_reg(t, 0x0454, 1 << 7, 0x0)                      # disable MAC CCK check
    set_bb_reg(t, 0x0A80, 1 << 18, 0x0)                     # disable BB CCK check
    set_bb_reg(t, 0x0814, 0x0000FC00, 15)                  # CCA mask (default)
    rf18 = (rf18 & ~((1 << 16) | (1 << 9) | (1 << 8))) & ~0xFF | central_ch
    _switch_rf_set(t, info.default_rf_set)
    write_rf_masked(t, 0xDF, 1 << 6, 0x1)                  # RF TXA_TANK LUT mode
    write_rf_masked(t, 0x64, 0xF, 0xF)                     # RF TXA_PA_TANK
    ts = dm.TrxStop()
    dm.stop_ic_trx(t, True, ts)
    write_rf(t, 0x18, rf18)
    dm.stop_ic_trx(t, False, ts)


def _switch_band_5g(t, central_ch: int) -> None:
    """config_phydm_switch_band_8821c [SRC] phydm_hal_api8821c.c:756 (5G arm). The mirror of the
    2.4 GHz arm: disable the CCK block + re-enable the CCK checks (5G has no CCK), set the CCA mask,
    set the RF band/channel word (RF 0x18 |= BIT16|BIT8 = 5G), route the RF set to WLA, clear the
    RF TXA_TANK LUT-mode bit, then write RF 0x18 with TRX stopped. No RF 0x64 write on the 5G arm."""
    rf18 = read_rf(t, 0x18)
    set_bb_reg(t, 0x0A80, 1 << 18, 0x1)                     # enable BB CCK check
    set_bb_reg(t, 0x0454, 1 << 7, 0x1)                      # enable MAC CCK check
    set_bb_reg(t, 0x0808, 1 << 28, 0x0)                     # disable CCK block
    set_bb_reg(t, 0x0814, 0x0000FC00, 15)                  # CCA mask (default)
    rf18 = (rf18 & ~((1 << 16) | (1 << 9) | (1 << 8))) | (1 << 8) | (1 << 16)
    rf18 = (rf18 & ~0xFF) | central_ch
    _switch_rf_set(t, _SWITCH_TO_WLA)
    write_rf_masked(t, 0xDF, 1 << 6, 0x0)                  # RF TXA_TANK LUT mode off
    ts = dm.TrxStop()
    dm.stop_ic_trx(t, True, ts)
    write_rf(t, 0x18, rf18)
    dm.stop_ic_trx(t, False, ts)


def _set_bb_swing_by_band_5g(t) -> None:
    """phy_set_bb_swing_by_band_8821c [SRC] rtl8821c_phy.c (5G band) — 0xc1c[31:21] = tx BB swing.
    The 0 dB registry swing yields 0x200 (same as 2.4 GHz on this card). (The companion
    `odm_clear_txpowertracking_state` is software — its thermal-baseline reset is applied to the
    watchdog state by the gate's band-switch handler, see RTL8821CU_DKMS.md.)"""
    set_bb_reg(t, 0x0C1C, 0xFFE00000, 0x200)


def _switch_channel_5g(t, central_ch: int, cut: int) -> None:
    """config_phydm_switch_channel_8821c [SRC] phydm_hal_api8821c.c:865 (5G arm): set the RF band/
    channel word (RF 0x18 + the >64 sub-band bits BIT17/BIT18), the AGC table index by sub-band
    (0xc1c[11:8]: 36-64->1, 100-144->2, >=149->3), the clock-offset central frequency (0x860), then
    write RF 0x18 with TRX stopped. No CCK-TX-filter (5G has no CCK). An A-cut part additionally
    read-modify-writes RF 0xb8[19] for the 5285-5375 MHz (ch 57-75) LCK-fail fix [SRC] :831-950 —
    gated on the runtime cut, so the pcap card (cut 4) never touches RF 0xb8 (byte-identical)."""
    rf18 = read_rf(t, 0x18)
    rf_b8 = read_rf(t, 0xB8) if cut == _ODM_CUT_A else 0    # [SRC] :831 A-cut only
    rf18 = (rf18 & ~((1 << 18) | (1 << 17) | 0xFF)) | central_ch
    if 100 <= central_ch <= 140:
        rf18 |= 1 << 17
    elif central_ch > 140:
        rf18 |= 1 << 18
    if 36 <= central_ch <= 64:
        set_bb_reg(t, 0x0C1C, 0x00000F00, 0x1)
    elif 100 <= central_ch <= 144:
        set_bb_reg(t, 0x0C1C, 0x00000F00, 0x2)
    else:                                                   # >= 149
        set_bb_reg(t, 0x0C1C, 0x00000F00, 0x3)
    if 36 <= central_ch <= 48:
        fc = 0x494
    elif 52 <= central_ch <= 64:
        fc = 0x453
    elif 100 <= central_ch <= 116:
        fc = 0x452
    else:                                                   # 118-177
        fc = 0x412
    set_bb_reg(t, 0x0860, 0x1FFE0000, fc)
    if cut == _ODM_CUT_A:                                   # [SRC] :919-924 A-cut LCK fix 0xb8[19]
        rf_b8 = (rf_b8 & ~(1 << 19)) if 57 <= central_ch <= 75 else (rf_b8 | (1 << 19))
    if central_ch == 153:
        _csi_mask_setting_5760(t, central_ch)              # FUNC_ENABLE: notch the 5760 MHz spur
    else:
        _csi_mask_disable(t)                               # FUNC_DISABLE (every other 5G channel)
    ts = dm.TrxStop()
    dm.stop_ic_trx(t, True, ts)
    write_rf(t, 0x18, rf18)
    dm.stop_ic_trx(t, False, ts)
    if cut == _ODM_CUT_A:                                   # [SRC] :949-950 write RF 0xb8
        write_rf(t, 0xB8, rf_b8)


# --- CSI-mask 5760 MHz spur notch (5G ch 151/153/155) [SRC] phydm_api.c:1190 -
_CSI_MASK_REG_P, _CSI_MASK_REG_N = 0x0880, 0x0890   # 11AC positive / negative tone banks
_CSI_MASK_TONE_NUM = 128                             # 11AC FFT tone count
_REG_CSI_MASK_EN = 0x0874                            # [0] = CSI-mask enable (11AC)
_SPUR_5760 = 5760                                    # the notched interferer (MHz)


def _csi_mask_enable(t, enable: bool) -> None:
    """phydm_csi_mask_enable (11AC) [SRC] phydm_api.c:866 — 0x874[0] = on/off."""
    set_bb_reg(t, _REG_CSI_MASK_EN, 1 << 0, 1 if enable else 0)


def _csi_mask_disable(t) -> None:
    """phydm_csi_mask_setting(FUNC_DISABLE) -> phydm_clean_all_csi_mask (11AC) [SRC] phydm_api.c:1190
    / :890 — clear the 8 CSI-mask dwords (0x880-0x89c) then disable 0x874[0]. Every 5G channel except
    the 5760 MHz spur notch channels (151/153/155) takes this."""
    for reg in range(_CSI_MASK_REG_P, _CSI_MASK_REG_P + 0x20, 4):
        set_bb_reg(t, reg, _MASKDWORD, 0)
    _csi_mask_enable(t, False)


def _set_csi_mask(t, tone_idx: int, positive: bool) -> None:
    """phydm_set_csi_mask (11AC) [SRC] phydm_api.c:929 — round the raw tone index to the nearest
    tone (/10), pick the positive (0x880+) or negative (0x890+, reflected through tone_num=128) tone
    bank, and OR the per-tone bit into the target byte."""
    if tone_idx % 10 >= 5:
        tone_idx += 10
    tone_idx //= 10
    if positive:
        tone_idx = min(tone_idx, _CSI_MASK_TONE_NUM - 1)
        target = _CSI_MASK_REG_P + (tone_idx >> 3)
    else:
        tone_idx = _CSI_MASK_TONE_NUM - min(tone_idx, _CSI_MASK_TONE_NUM)
        target = _CSI_MASK_REG_N + (tone_idx >> 3)
    t.write8(target, t.read8(target) | (1 << (tone_idx & 0x7)))


def _csi_mask_setting_5760(t, central_ch: int) -> None:
    """phydm_csi_mask_setting(FUNC_ENABLE, f_intf=5760) [SRC] phydm_hal_api8821c.c:927 ->
    phydm_api.c:1190 — notch the 5760 MHz spur on the 20 MHz channel 153. fc = 5180 + (ch-36)*5
    (phydm_find_fc, 5G/20 MHz); the interferer's tone distance to fc (<<5) drives phydm_set_csi_mask,
    then 0x874[0] enables the mask. The 40/80 MHz notch channels 151/155 are outside this driver's
    20 MHz hop scope, so only ch153 reaches here."""
    fc = 5180 + (central_ch - 36) * 5
    if not (fc - 10 <= _SPUR_5760 <= fc + 10):              # phydm_find_intf_distance in-band check (bw/2=10)
        _csi_mask_enable(t, False)                          # PHYDM_SET_NO_NEED -> mask off
        return
    tone_idx = abs(fc - _SPUR_5760) << 5
    _set_csi_mask(t, tone_idx, _SPUR_5760 >= fc)
    _csi_mask_enable(t, True)


def _set_bb_swing_by_band_2g(t) -> None:
    """phy_set_bb_swing_by_band_8821c [SRC] rtl8821c_phy.c — 0xc1c[31:21] = tx BB swing. The
    autoload-fail 2.4 GHz path with 0 dB registry swing yields 0x200 (no change)."""
    set_bb_reg(t, 0x0C1C, 0xFFE00000, 0x200)


def _switch_channel(t, central_ch: int, cut: int) -> None:
    """config_phydm_switch_channel_8821c [SRC] phydm_hal_api8821c.c:812 (2.4 GHz arm): set the RF
    band/channel word (RF 0x18), select AGC table 0 (0xc1c[11:8]), the clock-offset central
    frequency (0x860[28:17]=0x96a), and re-apply the cached CCK-TX-filter regs (ch != 14). An A-cut
    part additionally sets RF 0xb8[19] (LCK fix) [SRC] :831-950 — gated on the runtime cut, so the
    pcap card (cut 4) never touches RF 0xb8 (byte-identical)."""
    rf18 = read_rf(t, 0x18)
    rf_b8 = read_rf(t, 0xB8) if cut == _ODM_CUT_A else 0    # [SRC] :831 A-cut only
    rf18 = (rf18 & ~((1 << 18) | (1 << 17) | 0xFF)) | central_ch
    set_bb_reg(t, 0x0C1C, 0x00000F00, 0x0)                 # AGC table idx 0
    set_bb_reg(t, 0x0860, 0x1FFE0000, 0x96A)               # clock-offset fc
    if cut == _ODM_CUT_A:                                   # [SRC] :851-852 A-cut LCK fix 0xb8[19]=1
        rf_b8 |= 1 << 19
    if central_ch == 14:
        set_bb_reg(t, 0x0A24, _MASKDWORD, 0x0000b81c)
        set_bb_reg(t, 0x0A28, 0x0000FFFF, 0x0000)
        set_bb_reg(t, 0x0AAC, _MASKDWORD, 0x00003667)
    else:
        set_bb_reg(t, 0x0A24, _MASKDWORD, t.rega24)            # cached CCK TX filter
        set_bb_reg(t, 0x0A28, 0x0000FFFF, t.rega28 & 0xFFFF)
        set_bb_reg(t, 0x0AAC, _MASKDWORD, t.regaac)
    ts = dm.TrxStop()
    dm.stop_ic_trx(t, True, ts)
    write_rf(t, 0x18, rf18)
    dm.stop_ic_trx(t, False, ts)
    if cut == _ODM_CUT_A:                                   # [SRC] :949-950 write RF 0xb8
        write_rf(t, 0xB8, rf_b8)
    # phydm_ccapar_8821c is #if 0 (and cut != B) -> silent.


def _set_kfree_to_rf_2g(t, data: int) -> None:
    """phydm_set_kfree_to_rf_8821c(wlg_btg=TRUE) [SRC] halrf_kfree.c — enable the kfree gain
    override (RF 0xde[0], 0xde[5], 0x55[6], 0x65[6]) then load the WLG/BTG gain nibbles of ``data``
    into RF 0x55/0x65 [19] (lsb) + [18:14] (>>1)."""
    write_rf_masked(t, 0xDE, 1 << 0, 0x1)
    write_rf_masked(t, 0xDE, 1 << 5, 0x1)
    write_rf_masked(t, 0x55, 1 << 6, 0x1)
    write_rf_masked(t, 0x65, 1 << 6, 0x1)
    wlg, btg = data & 0xF, (data & 0xF0) >> 4
    write_rf_masked(t, 0x55, 1 << 19, wlg & 0x1)
    write_rf_masked(t, 0x55, _KFREE_GAIN_BMASK, wlg >> 1)
    write_rf_masked(t, 0x65, 1 << 19, btg & 0x1)
    write_rf_masked(t, 0x65, _KFREE_GAIN_BMASK, btg >> 1)


def _set_kfree_to_rf_5g(t, data: int) -> None:
    """phydm_set_kfree_to_rf_8821c(wlg_btg=FALSE) [SRC] halrf_kfree.c:214 — 5 GHz path A only:
    enable (RF 0xde[0]/[5], 0x55[6], 0x65[6]) then 0x55[19]=data[0] + 0x55[18:14]=(data&0x1f)>>1.
    Unlike the 2.4 GHz arm there is no 0x65 gain write (5G uses only RF 0x55)."""
    write_rf_masked(t, 0xDE, 1 << 0, 0x1)
    write_rf_masked(t, 0xDE, 1 << 5, 0x1)
    write_rf_masked(t, 0x55, 1 << 6, 0x1)
    write_rf_masked(t, 0x65, 1 << 6, 0x1)
    write_rf_masked(t, 0x55, 1 << 19, data & 0x1)
    write_rf_masked(t, 0x55, _KFREE_GAIN_BMASK, (data & 0x1F) >> 1)


def _config_kfree(t, info, channel: int) -> None:
    """phydm_config_kfree -> phydm_do_kfree [SRC] halrf_kfree.c:3666/3537 — apply the per-channel
    kfree gain. 8821C loads the 2.4 GHz PPG byte on a 2G channel (KFREE_FLAG_ON_2G) or the per-
    sub-band 5 GHz PPG byte on a 5G channel (KFREE_FLAG_ON_5G); both gated on KFREE_FLAG_ON."""
    if efuse.kfree_2g_gain(info) is None:                  # KFREE_FLAG_ON not set
        return
    if channel <= 14:                                      # KFREE_FLAG_ON_2G
        _set_kfree_to_rf_2g(t, efuse.kfree_2g_gain(info))
    else:                                                  # KFREE_FLAG_ON_5G
        _set_kfree_to_rf_5g(t, efuse.kfree_5g_gain(info, channel))


def _switch_bandwidth_20(t) -> None:
    """config_phydm_switch_bandwidth_8821c(20 MHz) [SRC] phydm_hal_api8821c.c:972: the 0x8ac
    BW/ADC-DAC-clock word (& 0xffcffc00 | 0x10010000), 0x8c4[30] ADC buffer clock, RF 0x18 |=
    BIT11|BIT10 under stopped TRX, then RX-DFIR (0x948/0x94c[29:28]=2, 0xc20[31]=1, 0x8f0[31]=0)
    and the BW-fixed indication (0x840[3:0]=pri_ch_idx 0, then 0x840[4]=enable). ccapar_by_bw /
    ccapar_8821c are #if 0 -> silent."""
    rf18 = read_rf(t, 0x18)
    t.write32(0x08AC, (t.read32(0x08AC) & 0xFFCFFC00) | 0x10010000)
    set_bb_reg(t, 0x08C4, 1 << 30, 0x1)
    rf18 |= (1 << 11) | (1 << 10)
    ts = dm.TrxStop()
    dm.stop_ic_trx(t, True, ts)
    write_rf(t, 0x18, rf18)
    dm.stop_ic_trx(t, False, ts)
    set_bb_reg(t, 0x0948, (1 << 29) | (1 << 28), 0x2)      # RX DFIR
    set_bb_reg(t, 0x094C, (1 << 29) | (1 << 28), 0x2)
    set_bb_reg(t, 0x0C20, 1 << 31, 0x1)
    set_bb_reg(t, 0x08F0, 1 << 31, 0x0)
    set_bb_reg(t, 0x0840, 0xF, 0x0)                        # bw_fixed_setting (pri_ch_idx 0)
    set_bb_reg(t, 0x0840, 1 << 4, 0x1)                     # bw_fixed_enable


def _mac_switch_bandwidth(t, channel: int, pri_ch_idx: int) -> None:
    """mac_switch_bandwidth [SRC] rtl8821c_phy.c:542 -> halmac cfg_ch_bw_88xx (20 MHz):
    cfg_pri_ch_idx (0x483 = txsc20 | txsc40<<4), cfg_bw (0x668 clears BIT7|8 for 20 MHz) +
    cfg_mac_clk (0x024[21:20]=80M-def(0), USTIME 0x55c/0x638 = MAC_CLK_SPEED 0x50), cfg_ch
    (0x454[7] = ch>35, 8-bit RMW). 0x454 is byte-wide here vs dword in switch_band."""
    txsc40 = 9 if pri_ch_idx in (1, 3) else 10
    t.write8(0x0483, (pri_ch_idx & 0xF) | ((txsc40 & 0xF) << 4))
    t.write32(0x0668, t.read32(0x0668) & ~((1 << 7) | (1 << 8)))          # cfg_bw 20 MHz
    t.write32(0x0024, t.read32(0x0024) & ~((1 << 20) | (1 << 21)))        # MAC clk 80M-def
    t.write8(0x055C, 0x50)
    t.write8(0x0638, 0x50)
    cck = t.read8(0x0454) & ~0x80
    t.write8(0x0454, cck | (0x80 if channel > 35 else 0))


def set_channel(t, info, channel: int) -> None:
    """rtl8821c_switch_chnl_and_set_bw [SRC] :740 (2.4 GHz, 20 MHz): band switch (coex notify +
    phydm band RF) only on a band change, channel RF, bandwidth, then tx-power. The first set
    switches band (forced invalid by init_hw_mlme_ext); same-band airodump hops skip it. For
    20 MHz center channel == channel."""
    central_ch = channel
    t.current_channel = channel
    is_5g = channel > 14
    # phy_switch_wireless_band_8821c [SRC] rtl8821c_phy.c:700 — band-switch sub-step, gated.
    if _need_switch_band(t, channel):
        t.thermal_reset_pending = True          # phy_set_bb_swing -> odm_clear_txpowertracking_state
        if is_5g:
            btc.switchband_notify_5g(t)
            _switch_band_5g(t, central_ch)
            _set_bb_swing_by_band_5g(t)
        else:
            btc.switchband_notify_2g(t)
            _switch_band(t, info, central_ch)
            _set_bb_swing_by_band_2g(t)
    if is_5g:
        _switch_channel_5g(t, central_ch, info.chip_ver)
    else:
        _switch_channel(t, central_ch, info.chip_ver)
    _config_kfree(t, info, channel)
    # set bandwidth (20 MHz, primary-channel index 0)
    _mac_switch_bandwidth(t, channel, 0)
    _switch_bandwidth_20(t)
    txpower.set_tx_power_level(t, info, channel)
