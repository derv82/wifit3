"""RTL8821AU (DKMS) channel tune — 2.4 GHz (M4) + 5 GHz (M7), 20 MHz primary.

Ports PHY_SwitchWirelessBand8812 (band switch) + PHY_SetSwChnlBWMode8812 (phy_SwChnl8812
-> phy_PostSetBwMode8812), 8821a / path-A / 20 MHz. `set_chnl_bw` is the connect-time
tune (an unconditional 2.4 GHz band switch, mirroring usb_halinit's
PHY_SwitchWirelessBand8812(2_4G) before rtw_hal_set_chnl_bw); `set_channel_bw` is the
runtime hop, where phy_SwBand reads the chip's band marker (REG_CCK_CHECK 0x454 BIT7)
and switches band only on a 2.4<->5 crossing. Per-rate TX power is applied separately
(txpower.set_tx_power / set_tx_power_5g) after the tune.

The 2.4 GHz RFE pinmux (phy_SetRFEReg8821) branches on the runtime ``ext_lna_2g`` fuse
(LNAType_2G[3]); the reference card reads 0 -> the external-LNA-bypass branch. bb_swing
reads 0x200 (0 dB / unburned fuse) on this card, both bands wire-confirmed.
# TODO: 40/80 MHz width.  # TODO(8812au): path-B is a real radio on 8812.
"""
from __future__ import annotations

from .rf import RF_CHNLBW, RF_PATH_A, RF_PATH_B, set_bb, set_rf_reg

BB_SWING_DEFAULT = 0x200   # 0 dB (efuse-unburned fallback)
_RF_MOD_AG = (1 << 18) | (1 << 17) | (1 << 16) | (1 << 9) | (1 << 8)   # RF_CHNLBW band bits
REG_CCK_CHECK = 0x0454
REG_TXPKT_EMPTY = 0x041A


def _ext_band_switch(t, band_5g: bool) -> None:
    """[SRC] phydm_set_ext_band_switch_8821A — 8811au 1-antenna band DPDT control."""
    set_bb(t, 0x004C, 1 << 23, 0)            # DPDT_P/N as output pin
    set_bb(t, 0x004C, 1 << 24, 1)            # by WLAN control
    set_bb(t, 0x0CB4, 0x0000000F, 7)         # DPDT_P
    set_bb(t, 0x0CB4, 0x000000F0, 7)         # DPDT_N
    set_bb(t, 0x0CB4, (1 << 29) | (1 << 28), 2 if band_5g else 1)  # band = 2b'10 (5G) / 2b'01 (2.4G)


def _set_rfe_2g(t, ext_lna_2g: bool) -> None:
    """[SRC] phy_SetRFEReg8821(BAND_ON_2_4G) — RFE pinmux, ExternalLNA_2G-gated.

    Turn off RF PA/LNA (0xCB0[15:12]=7, [7:4]=7), then either turn ON the 2.4 GHz external
    LNA (0xCB4 BIT20=1, pinmux [2:0]/[10:8]=b'010) or bypass it (BIT20=0, pinmux=b'111). The
    reference AWUS036ACS reads ExternalLNA_2G=0 -> the bypass branch (byte-identical to the
    former hardcode). The 5 GHz RFE (phy_SetRFEReg8821 else-branch) has no external-LNA
    branch, so _switch_band_5g stays card-independent.
    """
    set_bb(t, 0x0CB0, 0x0000F000, 0x7)       # 0xCB0[15:12]=0x7 (LNA_On)
    set_bb(t, 0x0CB0, 0x000000F0, 0x7)       # 0xCB0[7:4]=0x7 (PAPE_A)
    if ext_lna_2g:                           # turn ON 2.4G external LNA
        set_bb(t, 0x0CB4, 0x00100000, 0x1)   # 0xCB4 BIT20=1
        set_bb(t, 0x0CB4, 0x00400000, 0x0)   # 0xCB4 BIT22=0
        set_bb(t, 0x0CB0, 0x00000007, 0x2)   # 0xCB0[2:0]=b'010
        set_bb(t, 0x0CB0, 0x00000700, 0x2)   # 0xCB0[10:8]=b'010
    else:                                    # bypass external LNA (reference)
        set_bb(t, 0x0CB4, 0x00100000, 0x0)   # 0xCB4 BIT20=0
        set_bb(t, 0x0CB4, 0x00400000, 0x0)   # 0xCB4 BIT22=0
        set_bb(t, 0x0CB0, 0x00000007, 0x7)   # 0xCB0[2:0]=b'111
        set_bb(t, 0x0CB0, 0x00000700, 0x7)   # 0xCB0[10:8]=b'111


def _switch_band_2g(t, bb_swing: int, ext_lna_2g: bool = False) -> None:
    """[SRC] PHY_SwitchWirelessBand8812(BAND_ON_2_4G), 8821a path."""
    _ext_band_switch(t, band_5g=False)
    set_bb(t, 0x808, 0x30000000, 0x3)        # rOFDMCCKEN: OFDM + CCK on
    _set_rfe_2g(t, ext_lna_2g)               # phy_SetRFEReg8821 (ExternalLNA_2G-gated)
    set_bb(t, 0x0C1C, 0x00000F00, 0x0)       # AGC table select (MP chip)
    set_bb(t, 0x080C, 0x000000F0, 0x1)       # rTxPath
    set_bb(t, 0x0A04, 0x0F000000, 0x1)       # rCCK_RX
    _update_tx_basic_rate(t, 0x015F)         # 2.4G 11BG basic rates
    t.write8(REG_CCK_CHECK, t.read8(REG_CCK_CHECK) & ~0x80)  # clear BIT7 (2.4 GHz)
    set_bb(t, 0x0C1C, 0xFFE00000, bb_swing)  # bb_swing path A
    set_bb(t, 0x0E1C, 0xFFE00000, bb_swing)  # bb_swing path B (unconditional)


def _switch_band_5g(t, bb_swing: int) -> None:
    """[SRC] PHY_SwitchWirelessBand8812(BAND_ON_5G), 8821a path. The phy_SwBand band
    marker (REG_CCK_CHECK BIT7) is read by the caller; this sets it for 5 GHz."""
    _ext_band_switch(t, band_5g=True)
    # phy_SetRFEReg8821 (5 GHz): turn ON RF PA + LNA
    set_bb(t, 0x0CB0, 0x0000F000, 0x5)
    set_bb(t, 0x0CB0, 0x000000F0, 0x4)
    set_bb(t, 0x0CB4, 0x00100000, 0x0)
    set_bb(t, 0x0CB4, 0x00400000, 0x0)
    set_bb(t, 0x0CB0, 0x00000007, 0x7)
    set_bb(t, 0x0CB0, 0x00000700, 0x7)
    t.write8(REG_CCK_CHECK, t.read8(REG_CCK_CHECK) | 0x80)   # set BIT7 (5 GHz)
    # Wait for TX FIFO idle before the band swap (count loop; self-aligns to the wire).
    count = 0
    while (t.read16(REG_TXPKT_EMPTY) & 0x30) != 0x30 and count < 50:
        count += 1
    set_bb(t, 0x808, 0x30000000, 0x3)        # rOFDMCCKEN: OFDM + CCK on
    set_bb(t, 0x0C1C, 0x00000F00, 0x1)       # AGC table select (5 GHz, MP chip)
    set_bb(t, 0x080C, 0x000000F0, 0x0)       # rTxPath
    set_bb(t, 0x0A04, 0x0F000000, 0xF)       # rCCK_RX
    _update_tx_basic_rate(t, 0x0150)         # 5G 11A basic rates (OFDM only)
    set_bb(t, 0x0C1C, 0xFFE00000, bb_swing)  # bb_swing path A
    set_bb(t, 0x0E1C, 0xFFE00000, bb_swing)  # bb_swing path B (unconditional)


def _update_tx_basic_rate(t, brate_cfg: int) -> None:
    # [SRC] update_tx_basic_rate -> HW_VAR_BASIC_RATE: RRSR rate bitmap (masked RMW) +
    # clear the RRSR+2 low nibble.
    temp = (t.read32(0x0440) & 0xFFFF0000) | brate_cfg
    set_bb(t, 0x0440, 0x000FFFFF, temp)
    t.write8(0x0442, t.read8(0x0442) & 0xF0)


def _fc_area(t, ch: int) -> None:
    """[SRC] phy_SwChnl8812 fc_area (0x860[28:17])."""
    if 36 <= ch <= 48:
        v = 0x494
    elif 50 <= ch <= 64:
        v = 0x453
    elif 100 <= ch <= 116:
        v = 0x452
    elif ch >= 118:
        v = 0x412
    else:
        v = 0x96A
    set_bb(t, 0x0860, 0x1FFE0000, v)


def _rf_mod_ag(t, ch: int) -> None:
    """[SRC] phy_SwChnl8812 RF_MOD_AG band bits (RF 0x18[18:16,9:8])."""
    if 36 <= ch <= 64:
        v = 0x101
    elif 100 <= ch <= 140:
        v = 0x301
    elif ch > 140:
        v = 0x501
    else:
        v = 0x000
    set_rf_reg(t, RF_PATH_A, RF_CHNLBW, _RF_MOD_AG, v)


def _sw_chnl(t, ch: int, bb_swing_2g: int, bb_swing_5g: int, ext_lna_2g: bool = False) -> None:
    """[SRC] phy_SwChnl8812: phy_SwBand (conditional band switch) + fc_area + channel."""
    cur_5g = bool(t.read8(REG_CCK_CHECK) & 0x80)   # phy_SwBand8812 band marker
    want_5g = ch > 14
    if want_5g and not cur_5g:
        _switch_band_5g(t, bb_swing_5g)
    elif cur_5g and not want_5g:
        _switch_band_2g(t, bb_swing_2g, ext_lna_2g)
    _fc_area(t, ch)
    _rf_mod_ag(t, ch)
    set_rf_reg(t, RF_PATH_A, RF_CHNLBW, 0xFF, ch)   # channel byte0


def _post_set_bw_20(t) -> None:
    t.write16(0x0668, t.read16(0x0668) & 0xFE7F)   # phy_SetRegBW_8812: clear BIT7,8
    t.write8(0x0483, 0x00)                          # REG_DATA_SC: 20 MHz, secondary=0
    t.read8(0x0837)                                 # reg_837 (read; used only for 40/80 L1pk)
    set_bb(t, 0x08AC, 0x003003C3, 0x00300200)
    set_bb(t, 0x08C4, 0x40000000, 0x0)
    set_bb(t, 0x0848, 0x03C00000, 0x8)
    # PHY_RF6052SetBandwidth8812 (CH20): RF 0x18[11:10]=3, both paths (path B unconditional)
    set_rf_reg(t, RF_PATH_A, RF_CHNLBW, (1 << 11) | (1 << 10), 3)
    set_rf_reg(t, RF_PATH_B, RF_CHNLBW, (1 << 11) | (1 << 10), 3)


def set_chnl_bw(t, ch: int = 1, bb_swing_2g: int = BB_SWING_DEFAULT,
                ext_lna_2g: bool = False) -> None:
    """Connect-time tune (M4): unconditional 2.4 GHz band switch + channel + 20 MHz BW."""
    _switch_band_2g(t, bb_swing_2g, ext_lna_2g)
    _sw_chnl(t, ch, bb_swing_2g, BB_SWING_DEFAULT, ext_lna_2g)
    _post_set_bw_20(t)


def set_channel_bw(t, ch: int, bb_swing_2g: int = BB_SWING_DEFAULT,
                   bb_swing_5g: int = BB_SWING_DEFAULT, ext_lna_2g: bool = False) -> None:
    """Runtime hop (M7): phy_SwChnl (band switch only on a 2.4<->5 crossing) + channel + BW."""
    _sw_chnl(t, ch, bb_swing_2g, bb_swing_5g, ext_lna_2g)
    _post_set_bw_20(t)
