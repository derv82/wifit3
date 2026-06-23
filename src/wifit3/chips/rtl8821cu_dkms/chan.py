"""RTL8821CU channel tune — the phydm band/channel/bandwidth set the airmon channel hop drives.

[SRC] core/rtw_wlan_util.c:489 set_channel_bwmode -> rtw_hal_set_chnl_bw -> rtl8821c_set_channel_bw
([SRC] rtl8821c_phy.c:919) -> rtl8821c_switch_chnl_and_set_bw ([SRC] :740). For a 20 MHz channel the
center channel equals the primary channel. The first set (from ``init_hw_mlme_ext``) is channel 1,
2.4 GHz; the same path replays per airodump hop. Values are computed from the read-back RF/BB state,
not transcribed.
"""
from __future__ import annotations

from . import btc, dm
from .bb import set_bb_reg
from .rf import read_rf, write_rf, write_rf_masked

_MASKDWORD = 0xFFFFFFFF


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


def set_channel(t, info, channel: int) -> None:
    """rtl8821c_switch_chnl_and_set_bw [SRC] :740 (2.4 GHz, 20 MHz): band switch (coex notify +
    phydm band RF), channel RF, bandwidth, then tx-power. ``need_switch_band`` is TRUE on the first
    set (band forced to BAND_MAX by init_hw_mlme_ext); for 20 MHz center channel == channel."""
    central_ch = channel
    # phy_switch_wireless_band_8821c [SRC] rtl8821c_phy.c:700
    btc.switchband_notify_2g(t)
    _switch_band(t, info, central_ch)
    _set_bb_swing_by_band_2g(t)
    # config_phydm_switch_channel
    _switch_channel(t, central_ch)
