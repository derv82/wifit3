"""RTL8821CU channel tune — the phydm band/channel/bandwidth set the airmon channel hop drives.

[SRC] core/rtw_wlan_util.c:489 set_channel_bwmode -> rtw_hal_set_chnl_bw -> rtl8821c_set_channel_bw
([SRC] rtl8821c_phy.c:919) -> rtl8821c_switch_chnl_and_set_bw ([SRC] :740). For a 20 MHz channel the
center channel equals the primary channel. The first set (from ``init_hw_mlme_ext``) is channel 1,
2.4 GHz; the same path replays per airodump hop. Values are computed from the read-back RF/BB state,
not transcribed.
"""
from __future__ import annotations

from . import btc, dm, efuse
from .bb import set_bb_reg
from .rf import read_rf, write_rf, write_rf_masked

_MASKDWORD = 0xFFFFFFFF
_KFREE_GAIN_BMASK = 0x7C000        # RF 0x55/0x65 [18:14] kfree gain field


def _switch_rf_set(t, info) -> None:
    """config_phydm_switch_rf_set_8821c [SRC] phydm_hal_api8821c.c — route the RF front-end to the
    WLG/BTG/WLA set. This card's ``default_rf_set`` is BTG (combo, rfe 0x22): 0x1080[16]=1,
    0x00[26]=1, then merge the 0xcb8 select bits and load the BTG 0xa84/0xa80 gains. ``mp_mode`` is
    off so the AGC-diff retune is skipped."""
    set_bb_reg(t, 0x1080, 1 << 16, 0x1)
    set_bb_reg(t, 0x0000, 1 << 26, 0x1)
    bb = t.read32(0x0CB8)
    if info.default_rf_set == 0:                            # SWITCH_TO_BTG
        bb = (bb | (1 << 16)) & ~((1 << 18) | (1 << 20) | (1 << 21) | (1 << 22) | (1 << 23))
        set_bb_reg(t, 0x0A84, 0x00FF0000, 0xE)
        set_bb_reg(t, 0x0A80, 0x0000FFFF, 0xFC84)
    else:                                                   # SWITCH_TO_WLG
        bb = (bb | (1 << 20) | (1 << 21) | (1 << 22)) & ~((1 << 16) | (1 << 18) | (1 << 23))
        set_bb_reg(t, 0x0A84, 0x00FF0000, 0x12)
        set_bb_reg(t, 0x0A80, 0x0000FFFF, 0x7532)
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
    _switch_rf_set(t, info)
    write_rf_masked(t, 0xDF, 1 << 6, 0x1)                  # RF TXA_TANK LUT mode
    write_rf_masked(t, 0x64, 0xF, 0xF)                     # RF TXA_PA_TANK
    ts = dm.TrxStop()
    dm.stop_ic_trx(t, True, ts)
    write_rf(t, 0x18, rf18)
    dm.stop_ic_trx(t, False, ts)


def _set_bb_swing_by_band_2g(t) -> None:
    """phy_set_bb_swing_by_band_8821c [SRC] rtl8821c_phy.c — 0xc1c[31:21] = tx BB swing. The
    autoload-fail 2.4 GHz path with 0 dB registry swing yields 0x200 (no change)."""
    set_bb_reg(t, 0x0C1C, 0xFFE00000, 0x200)


def _switch_channel(t, central_ch: int) -> None:
    """config_phydm_switch_channel_8821c [SRC] phydm_hal_api8821c.c:812 (2.4 GHz arm): set the RF
    band/channel word (RF 0x18), select AGC table 0 (0xc1c[11:8]), the clock-offset central
    frequency (0x860[28:17]=0x96a), and re-apply the cached CCK-TX-filter regs (ch != 14)."""
    rf18 = read_rf(t, 0x18)
    rf18 = (rf18 & ~((1 << 18) | (1 << 17) | 0xFF)) | central_ch
    set_bb_reg(t, 0x0C1C, 0x00000F00, 0x0)                 # AGC table idx 0
    set_bb_reg(t, 0x0860, 0x1FFE0000, 0x96A)               # clock-offset fc
    set_bb_reg(t, 0x0A24, _MASKDWORD, t.rega24)            # cached CCK TX filter
    set_bb_reg(t, 0x0A28, 0x0000FFFF, t.rega28 & 0xFFFF)
    set_bb_reg(t, 0x0AAC, _MASKDWORD, t.regaac)
    ts = dm.TrxStop()
    dm.stop_ic_trx(t, True, ts)
    write_rf(t, 0x18, rf18)
    dm.stop_ic_trx(t, False, ts)
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


def _config_kfree(t, info, channel: int) -> None:
    """phydm_config_kfree -> phydm_do_kfree [SRC] halrf_kfree.c:3666/3537 — apply the per-channel
    kfree gain. 8821C 2.4 GHz uses the 2G PPG byte; when present (KFREE_FLAG_ON_2G) it loads the
    gain into RF (here gain 0, but the enable/gain RF writes still run)."""
    gain = efuse.kfree_2g_gain(info)
    if gain is None:                                       # KFREE_FLAG_ON not set
        return
    if channel <= 14:                                      # KFREE_FLAG_ON_2G
        _set_kfree_to_rf_2g(t, gain)


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
    phydm band RF), channel RF, bandwidth, then tx-power. ``need_switch_band`` is TRUE on the first
    set (band forced to BAND_MAX by init_hw_mlme_ext); for 20 MHz center channel == channel."""
    central_ch = channel
    # phy_switch_wireless_band_8821c [SRC] rtl8821c_phy.c:700
    btc.switchband_notify_2g(t)
    _switch_band(t, info, central_ch)
    _set_bb_swing_by_band_2g(t)
    _switch_channel(t, central_ch)
    _config_kfree(t, info, channel)
    # set bandwidth (20 MHz, primary-channel index 0)
    _mac_switch_bandwidth(t, channel, 0)
    _switch_bandwidth_20(t)
