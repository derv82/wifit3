"""RTL8812AU channel tune — 2.4 GHz (M4), 20 MHz primary, BOTH RF paths (2T2R).

Ports PHY_SwitchWirelessBand8812 + PHY_SetSwChnlBWMode8812 (phy_SwChnl8812 ->
phy_PostSetBwMode8812), 8812AU / 2T2R / 20 MHz. ``set_chnl_bw`` is the connect-time tune
(an unconditional 2.4 GHz band switch, mirroring usb_halinit's
PHY_SwitchWirelessBand8812(2_4G) before rtw_hal_set_chnl_bw); ``set_channel_bw`` is the
runtime hop, where phy_SwBand8812 reads the band marker (REG_CCK_CHECK 0x454 BIT7) and
switches band only on a 2.4<->5 crossing.

8812 deltas vs the 8821 2.4 GHz path: no 8811au DPDT ext-band-switch; the 8812-only
0x834[1:0]=1 and 0x830[17:13]=0x17 + 0x830[3:1]=4 (2T2R PWED_TH); rAGC_table 0x82C[1:0]=0
(normal chip, not the MP-chip 0xC1C[11:8]); phy_SetRFEReg8812 (both paths, RFE-type 0)
+ TxScale on 0xC1C AND 0xE1C; channel/RF_MOD/BW programmed on BOTH RF paths; L1PeakTH
0x848[25:22]=7 (2T2R, vs 1R's 8); and phy_FixSpur_8812A (2.4 GHz ADC-spur 0x8AC[9:8]).
The chip is C-cut (REG_SYS_CFG cut nibble = 1, +1 for the 8812 -> CUTVersion 2):
phy_FixSpur_8812A takes the C-cut branch (the 3-write spur path, run per-path in
_sw_chnl), and the B-cut rCCAonSec RF-read CCA toggle is correspondingly off (rf.py
clears t._rf_read_cca_off; the toggle itself lives in sipi._rf_serial_read).

bb_swing/rfe_type default to 0 dB / type-0 here; the EFUSE values are threaded in at
M-TXPWR. 5 GHz (M7) shares fc_area / RF_MOD_AG / FixSpur (C-cut, band-agnostic) and the
20 MHz L1PeakTH with 2.4 GHz; only the band switch (_switch_band_5g) + RFE + 11A basic
rate differ. wifit3 stays 20 MHz primary BY DESIGN — beacons / data / EAPOL all live in
the 20 MHz primary, so 40/80 MHz width is deliberately unported (a far-future capture-
bandwidth option, not a gap).
"""
from __future__ import annotations

from ..rtl88xxau_base.sipi import RF_CHNLBW, RF_PATH_A, RF_PATH_B, set_bb, set_rf_reg

BB_SWING_DEFAULT = 0x200   # 0 dB (efuse-unburned fallback)
_RF_MOD_AG = (1 << 18) | (1 << 17) | (1 << 16) | (1 << 9) | (1 << 8)   # RF_CHNLBW band bits
REG_CCK_CHECK = 0x0454
REG_TXPKT_EMPTY = 0x041A
REG_DATA_SC = 0x0483
_MASK_RFEINV = 0x3FF00000
_RFE_PINMUX = {RF_PATH_A: 0x0CB0, RF_PATH_B: 0x0EB0}
_RFE_INV = {RF_PATH_A: 0x0CB4, RF_PATH_B: 0x0EB4}
_TXSCALE = {RF_PATH_A: 0x0C1C, RF_PATH_B: 0x0E1C}


def _set_rfe_2g(t, rfe_type: int) -> None:
    """[SRC] phy_SetRFEReg8812(BAND_ON_2_4G) — RFE pinmux/inv for both paths.

    rfe_type 0/1/2 all resolve to pinmux 0x77777777 / inv 0x000 on this non-BT board;
    types 3-6 (external-PA/LNA / antenna-select boards) differ. Case 5 is a path-A partial
    write (pinmux byte2 + inv byte3 clear BIT0), mirroring the 5 GHz case-5 layout. rfe_type
    ∉ {0..6} follows the vendor switch `default:` (no-op). (TxScale is NOT part of this
    function — it is phy_SetBBSwingByBand_8812A, the band-switch tail; see below.)
    """
    if rfe_type == 3:
        set_bb(t, _RFE_PINMUX[RF_PATH_A], 0xFFFFFFFF, 0x54337770)
        set_bb(t, _RFE_PINMUX[RF_PATH_B], 0xFFFFFFFF, 0x54337770)
        set_bb(t, _RFE_INV[RF_PATH_A], _MASK_RFEINV, 0x010)
        set_bb(t, _RFE_INV[RF_PATH_B], _MASK_RFEINV, 0x010)
        set_bb(t, 0x0900, 0x00000303, 0x1)              # r_ANTSEL_SW
    elif rfe_type == 5:
        t.write8(_RFE_PINMUX[RF_PATH_A] + 2, 0x77)      # partial pinmux byte (path A)
        set_bb(t, _RFE_PINMUX[RF_PATH_B], 0xFFFFFFFF, 0x77777777)
        t.write8(_RFE_INV[RF_PATH_A] + 3, t.read8(_RFE_INV[RF_PATH_A] + 3) & ~0x01)  # inv byte3 &= ~BIT0
        set_bb(t, _RFE_INV[RF_PATH_B], _MASK_RFEINV, 0x000)
    elif rfe_type == 6:
        set_bb(t, _RFE_PINMUX[RF_PATH_A], 0xFFFFFFFF, 0x07772770)
        set_bb(t, _RFE_PINMUX[RF_PATH_B], 0xFFFFFFFF, 0x07772770)
        set_bb(t, _RFE_INV[RF_PATH_A], 0xFFFFFFFF, 0x00000077)
        set_bb(t, _RFE_INV[RF_PATH_B], 0xFFFFFFFF, 0x00000077)
    else:                                               # 0/1/2/4 -> 0x77777777
        inv = 0x001 if rfe_type == 4 else 0x000
        set_bb(t, _RFE_PINMUX[RF_PATH_A], 0xFFFFFFFF, 0x77777777)
        set_bb(t, _RFE_PINMUX[RF_PATH_B], 0xFFFFFFFF, 0x77777777)
        set_bb(t, _RFE_INV[RF_PATH_A], _MASK_RFEINV, inv)
        set_bb(t, _RFE_INV[RF_PATH_B], _MASK_RFEINV, inv)


def _set_rfe_5g(t, rfe_type: int) -> None:
    """[SRC] phy_SetRFEReg8812(BAND_ON_5G) — RFE pinmux/inv for both paths.

    The 5 GHz pinmux differs from 2.4 GHz per rfe_type (the AWUS036ACH is type 3 ->
    pinmux 0x54337717, inv 0x010, ANTSEL). Every vendor case is ported: unlike 2.4 GHz
    (where 0/1/2/4 share 0x77777777 and the sibling collapses them), the 5 GHz pinmux
    genuinely differs per type, so they can't be merged.
    """
    a, b = _RFE_PINMUX[RF_PATH_A], _RFE_PINMUX[RF_PATH_B]
    ia, ib = _RFE_INV[RF_PATH_A], _RFE_INV[RF_PATH_B]
    if rfe_type == 0:
        set_bb(t, a, 0xFFFFFFFF, 0x77337717)
        set_bb(t, b, 0xFFFFFFFF, 0x77337717)
        set_bb(t, ia, _MASK_RFEINV, 0x010)
        set_bb(t, ib, _MASK_RFEINV, 0x010)
    elif rfe_type == 1:
        set_bb(t, a, 0xFFFFFFFF, 0x77337717)
        set_bb(t, b, 0xFFFFFFFF, 0x77337717)
        set_bb(t, ia, _MASK_RFEINV, 0x000)
        set_bb(t, ib, _MASK_RFEINV, 0x000)
    elif rfe_type in (2, 4):
        set_bb(t, a, 0xFFFFFFFF, 0x77337777)
        set_bb(t, b, 0xFFFFFFFF, 0x77337777)
        set_bb(t, ia, _MASK_RFEINV, 0x010)
        set_bb(t, ib, _MASK_RFEINV, 0x010)
    elif rfe_type == 3:
        set_bb(t, a, 0xFFFFFFFF, 0x54337717)
        set_bb(t, b, 0xFFFFFFFF, 0x54337717)
        set_bb(t, ia, _MASK_RFEINV, 0x010)
        set_bb(t, ib, _MASK_RFEINV, 0x010)
        set_bb(t, 0x0900, 0x00000303, 0x1)              # r_ANTSEL_SW
    elif rfe_type == 5:
        t.write8(a + 2, 0x33)                           # partial pinmux byte (path A)
        set_bb(t, b, 0xFFFFFFFF, 0x77337777)
        t.write8(ia + 3, t.read8(ia + 3) | 0x01)        # inv byte3 BIT0
        set_bb(t, ib, _MASK_RFEINV, 0x010)
    elif rfe_type == 6:
        set_bb(t, a, 0xFFFFFFFF, 0x07737717)
        set_bb(t, b, 0xFFFFFFFF, 0x07737717)
        set_bb(t, ia, 0xFFFFFFFF, 0x00000077)
        set_bb(t, ib, 0xFFFFFFFF, 0x00000077)


def _set_bb_swing(t, bb_swing_a: int, bb_swing_b: int) -> None:
    """[SRC] phy_SetBBSwingByBand_8812A (rtl8812a_phycfg.c:1130), NORMAL_CHIP path — the
    per-band TxScale, written at the very end of PHY_SwitchWirelessBand8812 (both paths).
    """
    set_bb(t, _TXSCALE[RF_PATH_A], 0xFFE00000, bb_swing_a)   # 0xC1C[31:21]
    set_bb(t, _TXSCALE[RF_PATH_B], 0xFFE00000, bb_swing_b)   # 0xE1C[31:21]


def _switch_band_2g(t, bb_swing_a: int, bb_swing_b: int, rfe_type: int = 0) -> None:
    """[SRC] PHY_SwitchWirelessBand8812(BAND_ON_2_4G), 8812 path."""
    set_bb(t, 0x0808, 0x30000000, 0x3)       # rOFDMCCKEN: OFDM + CCK on
    set_bb(t, 0x0834, 0x00000003, 0x1)       # rBWIndication[1:0]=1 (8812)
    set_bb(t, 0x0830, 0x0003E000, 0x17)      # rPwed_TH[17:13]=0x17 (8812)
    set_bb(t, 0x0830, 0x0000000E, 0x04)      # rPwed_TH[3:1]=4 (2T2R / not 1T1R-noLNA)
    set_bb(t, 0x082C, 0x00000003, 0x0)       # rAGC_table[1:0]=0 (normal chip)
    _set_rfe_2g(t, rfe_type)                  # phy_SetRFEReg8812 (no TxScale)
    set_bb(t, 0x080C, 0x000000F0, 0x1)       # rTxPath (mp_mode==0)
    set_bb(t, 0x0A04, 0x0F000000, 0x1)       # rCCK_RX (mp_mode==0)
    _update_tx_basic_rate(t, 0x015F)         # WIRELESS_11BG basic rates
    t.write8(REG_CCK_CHECK, t.read8(REG_CCK_CHECK) & ~0x80)  # clear BIT7 (2.4 GHz)
    _set_bb_swing(t, bb_swing_a, bb_swing_b)  # phy_SetBBSwingByBand_8812A (band tail)


def _switch_band_5g(t, bb_swing_a: int, bb_swing_b: int, rfe_type: int = 0) -> None:
    """[SRC] PHY_SwitchWirelessBand8812(BAND_ON_5G), 8812 path (mp_mode==0). Unlike the
    2.4 GHz branch, the 5 GHz marker + a TX-FIFO-idle wait come FIRST, then the band regs,
    phy_SetRFEReg8812(5G), and the 11A basic-rate table (no CCK on 5 GHz).
    """
    t.write8(REG_CCK_CHECK, t.read8(REG_CCK_CHECK) | 0x80)   # set BIT7 (5 GHz marker)
    # Wait for the TX FIFO to drain (REG_TXPKT_EMPTY[5:4]==0b11) before the band swap,
    # bounded to 50 polls like the vendor. Dynamic-but-deterministic: under the replay
    # differ the capture's own recorded reads feed it, so the poll count matches the wire.
    count = 0
    while (t.read16(REG_TXPKT_EMPTY) & 0x30) != 0x30 and count < 50:
        count += 1
    set_bb(t, 0x0808, 0x30000000, 0x3)       # rOFDMCCKEN: OFDM + CCK on
    set_bb(t, 0x0834, 0x00000003, 0x2)       # rBWIndication[1:0]=2 (8812, 5 GHz)
    set_bb(t, 0x0830, 0x0003E000, 0x15)      # rPwed_TH[17:13]=0x15 (8812, 5 GHz)
    set_bb(t, 0x0830, 0x0000000E, 0x04)      # rPwed_TH[3:1]=4
    set_bb(t, 0x082C, 0x00000003, 0x1)       # rAGC_table[1:0]=1 (5 GHz, normal chip)
    _set_rfe_5g(t, rfe_type)                  # phy_SetRFEReg8812 (5 GHz, both paths)
    set_bb(t, 0x080C, 0x000000F0, 0x0)       # rTxPath (mp_mode==0)
    set_bb(t, 0x0A04, 0x0F000000, 0xF)       # rCCK_RX (mp_mode==0)
    _update_tx_basic_rate(t, 0x0150)         # WIRELESS_11A basic rates (OFDM only, no CCK)
    _set_bb_swing(t, bb_swing_a, bb_swing_b)  # phy_SetBBSwingByBand_8812A (band tail)


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


def _rf_mod_ag(t, path: int, ch: int) -> None:
    """[SRC] phy_SwChnl8812 RF_MOD_AG band bits (RF 0x18[18:16,9:8]), per path."""
    if 36 <= ch <= 64:
        v = 0x101
    elif 100 <= ch <= 140:
        v = 0x301
    elif ch > 140:
        v = 0x501
    else:
        v = 0x000
    set_rf_reg(t, path, RF_CHNLBW, _RF_MOD_AG, v)


def _fix_spur(t, ch: int, is_c_cut: bool = True) -> None:
    """[SRC] phy_FixSpur_8812A (rtl8812a_phycfg.c:1474), 20 MHz. Two silicon branches:

    C-cut (the captured card) writes the ADC-FIFO-clock fix 0x8AC[11:10]=2 (the 0x3 case is
    40 MHz ch11 only, never at 20 MHz), then the 2480 MHz ADC-clock workaround — ch 13/14 set
    0x8AC[9:8]=3 + 0x8C4[30]=1 (160M ADC), every other 2.4 GHz channel sets 0x8AC[9:8]=2 +
    0x8C4[30]=0. Non-C-cut 8812a does ONLY the 2480 MHz workaround, and only on 2.4 GHz
    (ch 13/14 -> 0x8AC[9:8]=3; ch<=14 -> 0x8AC[9:8]=2; 5 GHz untouched), with no 0x8C4[30].
    """
    if is_c_cut:
        set_bb(t, 0x08AC, 0x00000C00, 0x2)          # 0x8AC[11:10]
        if ch in (13, 14):
            set_bb(t, 0x08AC, 0x00000300, 0x3)      # 0x8AC[9:8]
            set_bb(t, 0x08C4, 0x40000000, 0x1)      # 0x8C4[30]
        else:
            set_bb(t, 0x08AC, 0x00000300, 0x2)
            set_bb(t, 0x08C4, 0x40000000, 0x0)
    elif ch in (13, 14):
        set_bb(t, 0x08AC, 0x00000300, 0x3)          # non-C-cut 8812a: 2.4 GHz ch 13/14
    elif ch <= 14:
        set_bb(t, 0x08AC, 0x00000300, 0x2)          # non-C-cut 8812a: other 2.4 GHz


def _sw_chnl(t, ch: int, bb_swing_2g_a: int, bb_swing_2g_b: int,
             bb_swing_5g_a: int, bb_swing_5g_b: int, rfe_type: int, is_c_cut: bool = True) -> None:
    """[SRC] phy_SwChnl8812: phy_SwBand (conditional band switch) + fc_area + per-path channel."""
    cur_5g = bool(t.read8(REG_CCK_CHECK) & 0x80)   # phy_SwBand8812 band marker
    want_5g = ch > 14
    if want_5g and not cur_5g:
        _switch_band_5g(t, bb_swing_5g_a, bb_swing_5g_b, rfe_type)
    elif cur_5g and not want_5g:
        _switch_band_2g(t, bb_swing_2g_a, bb_swing_2g_b, rfe_type)
    _fc_area(t, ch)
    for path in (RF_PATH_A, RF_PATH_B):            # 2T2R: both radios
        _rf_mod_ag(t, path, ch)
        _fix_spur(t, ch, is_c_cut)                 # phy_FixSpur_8812A (per-path, before chnl)
        set_rf_reg(t, path, RF_CHNLBW, 0xFF, ch)   # channel byte0


def _post_set_bw_20(t, ch: int, is_c_cut: bool = True) -> None:
    """[SRC] phy_PostSetBwMode8812 (CH20) + PHY_RF6052SetBandwidth8812 (CH20, both paths)."""
    t.write16(0x0668, t.read16(0x0668) & 0xFE7F)   # phy_SetRegBW_8812: clear BIT7,8
    t.write8(REG_DATA_SC, 0x00)                     # 20 MHz, secondary=0
    t.read8(0x0837)                                 # reg_837 (40/80 L1pk; read only)
    set_bb(t, 0x08AC, 0x003003C3, 0x00300200)
    set_bb(t, 0x08C4, 0x40000000, 0x0)
    set_bb(t, 0x0848, 0x03C00000, 0x7)              # 2T2R L1PeakTH (1R would be 8)
    _fix_spur(t, ch, is_c_cut)
    # PHY_RF6052SetBandwidth8812 (CH20): RF 0x18[11:10]=3, both paths
    set_rf_reg(t, RF_PATH_A, RF_CHNLBW, (1 << 11) | (1 << 10), 3)
    set_rf_reg(t, RF_PATH_B, RF_CHNLBW, (1 << 11) | (1 << 10), 3)


def set_chnl_bw(t, ch: int = 1, bb_swing_2g_a: int = BB_SWING_DEFAULT,
                bb_swing_2g_b: int = BB_SWING_DEFAULT, rfe_type: int = 0,
                is_c_cut: bool = True) -> None:
    """Connect-time tune (M4): unconditional 2.4 GHz band switch + channel + 20 MHz BW."""
    _switch_band_2g(t, bb_swing_2g_a, bb_swing_2g_b, rfe_type)
    _sw_chnl(t, ch, bb_swing_2g_a, bb_swing_2g_b, BB_SWING_DEFAULT, BB_SWING_DEFAULT, rfe_type,
             is_c_cut)
    _post_set_bw_20(t, ch, is_c_cut)


def set_channel_bw(t, ch: int, bb_swing_2g_a: int = BB_SWING_DEFAULT,
                   bb_swing_2g_b: int = BB_SWING_DEFAULT, bb_swing_5g_a: int = BB_SWING_DEFAULT,
                   bb_swing_5g_b: int = BB_SWING_DEFAULT, rfe_type: int = 0,
                   is_c_cut: bool = True) -> None:
    """Runtime hop (M7): phy_SwChnl (band switch only on a 2.4<->5 crossing) + channel + BW."""
    _sw_chnl(t, ch, bb_swing_2g_a, bb_swing_2g_b, bb_swing_5g_a, bb_swing_5g_b, rfe_type, is_c_cut)
    _post_set_bw_20(t, ch, is_c_cut)
