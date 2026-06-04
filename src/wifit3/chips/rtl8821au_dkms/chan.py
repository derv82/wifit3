"""RTL8821AU (DKMS) M4: 2.4 GHz band switch + channel select + 20 MHz BW.

Ported from PHY_SwitchWirelessBand8812(BAND_ON_2_4G) + PHY_SetSwChnlBWMode8812
(phy_SwChnl8812 -> phy_PostSetBwMode8812), 8821a / path-A-tune / 20 MHz. Per-rate
TX power (rtw_hal_set_tx_power_level, the tail of the vendor tune) is a separate
EFUSE-driven milestone and is NOT emitted here.

bb_swing reads 0x200 (0 dB / unburned fuse) on this card, wire-confirmed.
# TODO(efuse): read bb_swing_2g.  # TODO: 40/80 MHz width.  # TODO(8812au): 5 GHz band.
"""
from __future__ import annotations

from .rf import RF_CHNLBW, RF_PATH_A, RF_PATH_B, set_bb, set_rf_reg

BB_SWING_2G = 0x200   # 0 dB default (TODO(efuse): EEPROM_TX_BBSWING_2G)


def _switch_band_2g(t):
    # 8811au 1-antenna ext band switch (phydm_set_ext_band_switch_8821A, ODM_BAND_2_4G).
    set_bb(t, 0x004C, 1 << 23, 0)            # DPDT_P/N as output pin
    set_bb(t, 0x004C, 1 << 24, 1)            # by WLAN control
    set_bb(t, 0x0CB4, 0x0000000F, 7)         # DPDT_P
    set_bb(t, 0x0CB4, 0x000000F0, 7)         # DPDT_N
    set_bb(t, 0x0CB4, (1 << 29) | (1 << 28), 1)  # band switch = 2b'01 (2.4G)
    set_bb(t, 0x808, 0x30000000, 0x3)        # rOFDMCCKEN: OFDM + CCK on
    # phy_SetRFEReg8821 (2.4 GHz, ExternalLNA_2G=0)
    set_bb(t, 0x0CB0, 0x0000F000, 0x7)
    set_bb(t, 0x0CB0, 0x000000F0, 0x7)
    set_bb(t, 0x0CB4, 0x00100000, 0x0)
    set_bb(t, 0x0CB4, 0x00400000, 0x0)
    set_bb(t, 0x0CB0, 0x00000007, 0x7)
    set_bb(t, 0x0CB0, 0x00000700, 0x7)
    set_bb(t, 0x0C1C, 0x00000F00, 0x0)       # AGC table select (MP chip)
    set_bb(t, 0x080C, 0x000000F0, 0x1)       # rTxPath
    set_bb(t, 0x0A04, 0x0F000000, 0x1)       # rCCK_RX
    # update_tx_basic_rate -> HW_VAR_BASIC_RATE [SRC] hal_com.c:13237-13243.
    # temp = (RRSR & 0xFFFF0000) | BrateCfg; rtw_phydm_set_rrsr (masked RMW); clear RRSR+2 low nibble.
    temp_rrsr = (t.read32(0x0440) & 0xFFFF0000) | 0x015F   # BrateCfg = 2.4G 11BG basic rates
    set_bb(t, 0x0440, 0x000FFFFF, temp_rrsr)
    t.write8(0x0442, t.read8(0x0442) & 0xF0)
    t.write8(0x0454, t.read8(0x0454) & ~0x80)  # REG_CCK_CHECK clear BIT7 (2.4 GHz)
    set_bb(t, 0x0C1C, 0xFFE00000, BB_SWING_2G)  # bb_swing path A
    set_bb(t, 0x0E1C, 0xFFE00000, BB_SWING_2G)  # bb_swing path B (unconditional)


def _sw_chnl(t, ch):
    t.read8(0x0454)                          # phy_SwBand8812: read CCK band marker (same band)
    set_bb(t, 0x0860, 0x1FFE0000, 0x96A)     # fc_area (ch <= 35)
    # RF_MOD_AG (2.4 GHz) then channel byte0, path A
    set_rf_reg(t, RF_PATH_A, RF_CHNLBW, (1 << 18) | (1 << 17) | (1 << 16) | (1 << 9) | (1 << 8), 0x000)
    set_rf_reg(t, RF_PATH_A, RF_CHNLBW, 0xFF, ch)


def _post_set_bw_20(t):
    t.write16(0x0668, t.read16(0x0668) & 0xFE7F)   # phy_SetRegBW_8812: clear BIT7,8
    t.write8(0x0483, 0x00)                          # REG_DATA_SC: 20 MHz, secondary=0
    t.read8(0x0837)                                 # reg_837 (read; used only for 40/80 L1pk)
    set_bb(t, 0x08AC, 0x003003C3, 0x00300200)
    set_bb(t, 0x08C4, 0x40000000, 0x0)
    set_bb(t, 0x0848, 0x03C00000, 0x8)
    # PHY_RF6052SetBandwidth8812 (CH20): RF 0x18[11:10]=3, both paths (path B unconditional)
    set_rf_reg(t, RF_PATH_A, RF_CHNLBW, (1 << 11) | (1 << 10), 3)
    set_rf_reg(t, RF_PATH_B, RF_CHNLBW, (1 << 11) | (1 << 10), 3)


def set_chnl_bw(t, ch: int = 1) -> None:
    _switch_band_2g(t)
    _sw_chnl(t, ch)
    _post_set_bw_20(t)
