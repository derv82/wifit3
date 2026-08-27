"""RTL8814AU channel tune (M2d/M5a) — port of the vendor stack, 20 MHz primary only.

Mirrors the hal_init tail [SRC usb_halinit.c:1229-1237]:
    PHY_ConfigBB_8814A           enable OFDM + CCK
    PHY_SwitchWirelessBand8814A  switch to the 2.4 GHz band (5G branch = M5a)
    rtw_hal_set_chnl_bw(.., CHANNEL_WIDTH_20, ..) -> phy_SwChnl8814A (channel) +
                                 phy_SetBwMode8814A (20 MHz) + spur-cal reset

Per the 20-MHz-only scope, the 40/80 MHz width math is omitted. TX power
(rtw_hal_set_tx_power_level, the 0x1998 loop) and IQK follow in the vendor flow but
are TX/cal concerns deferred to a later milestone.

The 5 GHz band switch is ported (M5a: ``switch_wireless_band_5g`` +
``phy_sw_band``, the band-crossing dispatcher); the 5 GHz channel *select* (fc-area /
RF sub-band / AGC-table select) is M5b, so ``set_channel_bw`` still tunes 2.4 GHz only.

RF register writes/reads go through the memory-mapped interface in ``rf.py``.
Verified byte-for-byte; [WIRE] cap1 frames 13695-13855 (channel 1).
"""
from __future__ import annotations

from . import constants as C
from .bb import _set_reg_masked as _bb32
from .rf import set_rf_masked
from .txpower import set_tx_power, set_tx_power_5g

_RF_PATHS = ("a", "b", "c", "d")


def _bb8_clear_set(t, addr: int, bit: int, set_bit: bool) -> None:
    v = t.read8(addr)
    t.write8(addr, (v | bit) if set_bit else (v & ~bit))


def phy_config_bb(t) -> None:
    """[SRC] PHY_ConfigBB_8814A — enable OFDM + CCK (rOFDMCCKEN[29:28] = 0x3)."""
    _bb32(t, C.rOFDMCCKEN, C.bOFDMEN | C.bCCKEN, 0x3)


def _set_rfe_reg_2g(t, rfe_type: int = 1) -> None:
    """[SRC] PHY_SetRFEReg8814A(FALSE, 2.4G) — per-rfe_type RFE pinmux + inv nibble.

    A/B/C share one pinmux word; D differs (or is skipped for rfe 0/default, which writes
    A/B/C only). rfe_type ∉ {1,2} falls to the switch `default:` (== rfe 0). The captured
    card is rfe_type=1 (all four = 0x77777777, inv 0x77)."""
    a_c, d_val, inv = C.RFE_PINMUX_2G.get(rfe_type, C.RFE_PINMUX_2G_DEFAULT)
    for reg in C.RFE_PINMUX[:3]:
        t.write32(reg, a_c)
    if d_val is not None:
        t.write32(C.RFE_PINMUX[3], d_val)
    _bb32(t, C.REG_RFE_INV, 0x0FF00000, inv)


def set_rfe_reg_init(t, rfe_type: int) -> None:
    """[SRC] PHY_SetRFEReg8814A(bInit=TRUE) — RFE control enable + GPIO antenna-select.

    Run once from the hal_init turn-on block. The vendor switch has cases 0/1/2 only (no
    default): rfe 1/2 enable 0x1994[3:0]=0xf and set GPIO 0x42[23:20]=0xf (|0xf0); rfe 0
    sets GPIO 0x42[23:22]=2b'11 (|0xc0). Any other rfe_type is a no-op on the wire (the
    vendor leaves 0x1994 + GPIO untouched) — an untested variant, run as the vendor would."""
    if rfe_type not in (0, 1, 2):
        return
    _bb32(t, C.RFE_8814_REG, 0xF, 0xF)
    gpio_bits = 0xF0 if rfe_type in (1, 2) else 0xC0
    v = t.read8(C.REG_GPIO_IO_SEL_8814A)
    t.write8(C.REG_GPIO_IO_SEL_8814A, v | gpio_bits)


def _set_bb_swing(t, bb_swing: tuple) -> None:
    """[SRC] phy_SetBBSwingByBand_8814A — per-path TxScale[31:21] (band-neutral writer).

    The four TxScale registers are identical across bands; only the per-path 11-bit
    value differs (decoded from efuse 0xC6 for 2.4 GHz, 0xC7 for 5 GHz). The caller
    passes its band's tuple; on an unburned fuse every path is the 0 dB default (0x200),
    which is what this card reads. The vendor's ``BBDiffBetweenBand`` / OFDM-index update
    and ``odm_clear_txpowertracking_state`` are software DM state (no register I/O).
    """
    for reg, val in zip(C.TXSCALE, bb_swing):
        _bb32(t, reg, C.BBSWING_MASK, val)


def _set_bw_reg_adc_agc_20(t) -> None:
    """[SRC] phy_SetBwRegAdc_8814A / phy_SetBwRegAgc_8814A for CHANNEL_WIDTH_20."""
    _bb32(t, C.rRFMOD, 0x3, 0x0)              # ADC: 0x8ac[1:0] = 0
    _bb32(t, C.rAGC_table_Jaguar, 0xF000, 0x6)  # AGC: 0x82c[15:12] = 6


def switch_wireless_band_2g(t, bb_swing: tuple, rfe_type: int = 1) -> None:
    """[SRC] PHY_SwitchWirelessBand8814A(BAND_ON_2_4G), 20 MHz, mp_mode=0."""
    _bb8_clear_set(t, C.REG_SYS_CFG3_2, 0x01, False)   # gate CCK/OFDM clock off
    _bb32(t, C.rAGC_table_Jaguar2, 0x1F, 0x0)          # 2.4G AGC table select
    _set_rfe_reg_2g(t, rfe_type)
    _bb32(t, C.rTxPath, 0xF0, 0x2)
    _bb32(t, C.rCCK_RX, 0x0F000000, 0x5)
    _bb32(t, C.rOFDMCCKEN, C.bOFDMEN | C.bCCKEN, 0x3)
    t.write8(C.REG_CCK_CHECK, 0x0)
    _bb32(t, C.REG_A80, 1 << 18, 0x0)
    _set_bb_swing(t, bb_swing)
    _set_bw_reg_adc_agc_20(t)
    _bb8_clear_set(t, C.REG_SYS_CFG3_2, 0x01, True)     # gate CCK/OFDM clock on


def _set_rfe_reg_5g(t, rfe_type: int = 1) -> None:
    """[SRC] PHY_SetRFEReg8814A(FALSE, 5G) — per-rfe_type RFE pinmux + inv nibble.

    Paths A/B/C share one word; path D differs (rfe 1/2). rfe_type ∉ {1,2} falls to the
    switch `default:` (== rfe 0: all four = 0x54775477, inv 0x54). rfe_type=1 (captured):
    A/B/C=0x33173317, D=0x77177717, inv 0x33 (vs 0x77 on 2.4 GHz).
    """
    a_c, d_val, inv = C.RFE_PINMUX_5G.get(rfe_type, C.RFE_PINMUX_5G_DEFAULT)
    for reg in C.RFE_PINMUX[:3]:
        t.write32(reg, a_c)
    t.write32(C.RFE_PINMUX[3], d_val)
    _bb32(t, C.REG_RFE_INV, 0x0FF00000, inv)


def switch_wireless_band_5g(t, bb_swing: tuple, rfe_type: int = 1) -> None:
    """[SRC] PHY_SwitchWirelessBand8814A(BAND_ON_5G), 20 MHz, mp_mode=0.

    Differs from the 2.4 GHz branch in both values and order: the CCK_CHECK bit7 band
    marker (0x80) and the CCK-Tx-enable 0xa80[18] are written FIRST (before RFE), CCK is
    left OFF (rOFDMCCKEN = OFDM-only 0x2), and the 0x958 AGC-table select is DEFERRED to
    the channel switch (M5b). The shared BB-swing / ADC-AGC / clock-gate suffix reuses the
    2.4 GHz helpers — the ADC/AGC 20 MHz values are band-independent.
    """
    _bb8_clear_set(t, C.REG_SYS_CFG3_2, 0x01, False)   # gate CCK/OFDM clock off
    t.write8(C.REG_CCK_CHECK, 0x80)                    # CCK_CHECK bit7 = 5G band marker
    _bb32(t, C.REG_A80, 1 << 18, 0x1)                  # enable CCK Tx even when CCK is off
    # 0x958 AGC-table select is postponed to the channel switch (M5b).
    _set_rfe_reg_5g(t, rfe_type)
    _bb32(t, C.rTxPath, 0xF0, 0x0)
    _bb32(t, C.rCCK_RX, 0x0F000000, 0xF)
    _bb32(t, C.rOFDMCCKEN, C.bOFDMEN | C.bCCKEN, 0x2)   # OFDM only (CCK off)
    _set_bb_swing(t, bb_swing)
    _set_bw_reg_adc_agc_20(t)
    _bb8_clear_set(t, C.REG_SYS_CFG3_2, 0x01, True)     # gate CCK/OFDM clock on


def phy_sw_band(t, channel: int, bb_swing_2g: tuple, bb_swing_5g: tuple,
                current_band: int, rfe_type: int = 1) -> int:
    """[SRC] phy_SwBand8814A — switch the RF band only on a 2.4G<->5G crossing.

    The switch *decision* uses the chip's hardware band marker (REG_CCK_CHECK bit7: 5G if
    set), not software state — exactly as the vendor reads it. ``current_band`` is the
    vendor's lagging ``current_band_type`` software field, returned updated: it advances to
    the target band ONLY when PHY_SwitchWirelessBand8814A actually fires (a same-band tune
    leaves it untouched). That lag is what makes a post-init_hw_mlme_ext (BAND_MAX) 2.4 GHz
    tune skip CCK txagc. This is the first op of phy_SwChnl8814A.
    """
    cur_5g = bool(t.read8(C.REG_CCK_CHECK) & 0x80)     # bit7 = HW current band (5G if set)
    tgt_5g = channel > 14
    if tgt_5g == cur_5g:
        return current_band                            # no switch -> software band unchanged
    if tgt_5g:
        switch_wireless_band_5g(t, bb_swing_5g, rfe_type)
        return C.BAND_ON_5G
    switch_wireless_band_2g(t, bb_swing_2g, rfe_type)
    return C.BAND_ON_2_4G


def _fc_area(channel: int) -> int:
    """[SRC] phy_SwChnl8814A fc_area table — 0x860[28:17] center-freq area by sub-band."""
    if 36 <= channel <= 48:
        return 0x494
    if 50 <= channel <= 64:
        return 0x453
    if 100 <= channel <= 116:
        return 0x452
    if channel >= 118:
        return 0x412
    return 0x96A                              # 2.4 GHz (channel <= 35)


def _rf_mod_ag(channel: int) -> int:
    """[SRC] phy_SwChnl8814A RF_MOD_AG table — merged into RF 0x18 [18:16]/[9:8] per sub-band."""
    if 36 <= channel <= 64:
        return 0x101
    if 100 <= channel <= 140:
        return 0x301
    if channel > 140:
        return 0x501
    return 0x000                             # 2.4 GHz


def _phy_sw_chnl(t, channel: int, bb_swing_2g: tuple, bb_swing_5g: tuple,
                 current_band: int, rfe_type: int = 1) -> int:
    """[SRC] phy_SwChnl8814A — band switch (on a crossing) then channel select (2.4G / 5G).

    The fc-area / RF_MOD_AG / AGC-table-select block is one channel-range table spanning
    both bands (2.4 GHz falls out: fc_area 0x96A, RF_MOD_AG 0, no 0x958 write — the band
    switch already selected the 2.4G AGC table). The CCK TX-DFIR is 2.4 GHz only; 5 GHz
    matches no DFIR arm. The mp-mode-only spur/initial-gain block (phy_SpurCalibration +
    phy_ModifyInitialGain here) is skipped — this build is mp_mode=0; the runtime spur cal
    rides phy_SetBwMode (_spur_nbi). Returns the (possibly updated) software band.
    """
    new_band = phy_sw_band(t, channel, bb_swing_2g, bb_swing_5g, current_band, rfe_type)  # phy_SwBand8814A
    _bb32(t, C.rFc_area, 0x1FFE0000, _fc_area(channel))
    mod_ag = _rf_mod_ag(channel)
    for path in _RF_PATHS:                    # RF 0x18 = channel | (RF_MOD_AG << 8)
        set_rf_masked(t, path, C.RF_CHNLBW, C.RF_CHNLBW_CH_MASK, channel | (mod_ag << 8))
    # AGC-table select 0x958[4:0] — 5 GHz sub-bands only (1=36-64, 2=100-144, 3=>=149);
    # 2.4 GHz selected it (=0) in the band switch, so no write here.
    if 36 <= channel <= 64:
        _bb32(t, C.rAGC_table_Jaguar2, 0x1F, 1)
    elif 100 <= channel <= 144:
        _bb32(t, C.rAGC_table_Jaguar2, 0x1F, 2)
    elif channel >= 149:
        _bb32(t, C.rAGC_table_Jaguar2, 0x1F, 3)
    # 2.4G CCK TX DFIR (5 GHz matches no arm -> skipped)
    if channel <= 14:
        if 1 <= channel <= 11:
            f2, dbg = 0x090E1317, 0x00000204
        elif 12 <= channel <= 13:
            f2, dbg = 0x090E1217, 0x00000305
        elif channel == 14:
            f2, dbg = 0x00000E17, 0x00000000

        t.write32(C.rCCK0_TxFilter1, 0x1A1B0030)
        t.write32(C.rCCK0_TxFilter2, f2)
        t.write32(C.rCCK0_DebugPort, dbg)
    return new_band


# [SRC] phydm_set_nbi_reg nbi_128[] (tone_idx x10) — 8814A 20/40 MHz uses the FFT-128
# table. reg_idx = (first index whose entry exceeds tone_idx) + 1, written to 0x87c[19:14].
_NBI_128 = (25, 55, 85, 115, 135, 155, 185, 205, 225, 245, 265, 285, 305, 335, 355,
            375, 395, 415, 435, 455, 485, 505, 525, 555, 585, 615, 635)
# [SRC] phydm_spur_nbi_setting_8814a (rfe 0/1/6/7): the only ACTIVE NBI notches are the
# 2.4 GHz spurs — ch 4-8 (2440 MHz) and ch 14 (2480 MHz). The 5 GHz notch cases in that
# function are all #if 0, so every 5 GHz channel falls through to FUNC_DISABLE (disable
# NBI) — the same wire as a non-spur 2.4 GHz channel. (The one 5 GHz exception is the
# ch153 notch in phy_SpurCalibration_8814A itself, which is M5f.)
_SPUR_INTF = {4: 2440, 5: 2440, 6: 2440, 7: 2440, 8: 2440, 14: 2480}


def _nbi_reg_idx(channel: int, f_intf: int) -> int:
    """[SRC] phydm_find_fc + phydm_find_intf_distance + phydm_set_nbi_reg.

    fc = 2412 + (ch-1)*5; the interferer's tone index is (|fc - f_intf| << 5); reg_idx is
    its bin in the FFT-128 table. Verified vs the cold-boot wire (ch4->19, ch6->4, ch8->9).
    """
    fc = 2412 + (channel - 1) * 5
    tone_idx = abs(fc - f_intf) << 5
    for i, tone in enumerate(_NBI_128):
        if tone_idx < tone:
            return i + 1
    return 0


def _ch153_csi_notch(t) -> None:
    """[SRC] phy_SpurCalibration_8814A ch153 @ 20 MHz (rfe 0/1/2) — CSI notch on."""
    _bb32(t, C.rNBI_Setting, 0x000FE000, 0x1E >> 1)
    _bb32(t, C.rCSI_Mask_Setting1, 0x1, 0x1)
    t.write32(C.rCSI_FIX_MASK[0], 0x0)            # rCSI_Fix_Mask0
    t.write32(C.rCSI_FIX_MASK[1], 0x0)            # rCSI_Fix_Mask1
    t.write32(C.rCSI_FIX_MASK[2], 0x0)            # rCSI_Fix_Mask6
    _bb32(t, C.rCSI_FIX_MASK[3], 1 << 16, 0x1)    # rCSI_Fix_Mask7[16] = 1


def _reset_nbi_csi(t) -> None:
    """[SRC] phy_SpurCalibration_8814A Reset_NBI_CSI branch — reset the NBI tap + CSI masks."""
    _bb32(t, C.rNBI_Setting, 0x000FE000, 0xFC >> 1)
    _bb32(t, C.rCSI_Mask_Setting1, 0x1, 0x0)
    for reg in C.rCSI_FIX_MASK:
        t.write32(reg, 0x0)


def _spur_nbi(t, channel: int, rfe_type: int = 1) -> None:
    """[SRC] phy_SpurCalibration_8814A (CSI) + phydm_spur_nbi_setting_8814a (NBI), 20 MHz.

    Both halves are rfe_type-gated. CSI (phy_SpurCalibration_8814A): rfe 1/2 notch only
    ch153; rfe 0 also notches ch153 (its extra ch140 8814AE MP-Rx AGC tweak is NOT ported —
    stateful save/restore, DFS-only, an untested variant); rfe ∉ {0,1,2} always resets. NBI
    (phydm_spur_nbi_setting_8814a): rfe ∈ {0,1,6,7} tap+enable on a 2.4 GHz spur (ch 4-8 /
    ch 14) and disable elsewhere (incl. every 5 GHz channel); any other rfe_type leaves NBI
    untouched. The captured card (rfe 1) notches ch153, enables NBI on ch 4-8/14, disables
    otherwise — byte-diffed per channel by the single-cursor verify_pcap.
    """
    # phy_SpurCalibration_8814A (CHANNEL_WIDTH_20)
    if rfe_type in (0, 1, 2) and channel == 153:
        _ch153_csi_notch(t)
    else:
        _reset_nbi_csi(t)
    # phydm_spur_nbi_setting_8814a — only rfe ∈ {0,1,6,7} touches NBI
    if rfe_type in (0, 1, 6, 7):
        f_intf = _SPUR_INTF.get(channel)
        if f_intf is None:
            _bb32(t, C.rNBI_Setting, C.NBI_EN_BIT, 0x0)
        else:
            _bb32(t, C.rNBI_Setting, 0x000FC000, _nbi_reg_idx(channel, f_intf))
            _bb32(t, C.rNBI_Setting, C.NBI_EN_BIT, 0x1)


def _phy_set_bw_mode_20(t, channel: int, rfe_type: int = 1) -> None:
    """[SRC] phy_SetBwMode8814A — CHANNEL_WIDTH_20."""
    v = t.read16(C.REG_TRXPTCL_CTL)           # MAC bw: clear BIT7|BIT8
    t.write16(C.REG_TRXPTCL_CTL, v & ~((1 << 7) | (1 << 8)))
    t.write8(C.REG_DATA_SC, 0x0)              # secondary channel = 0
    _set_bw_reg_adc_agc_20(t)
    for path in _RF_PATHS:                    # RF bw: 0x18[11:10] = 3
        set_rf_masked(t, path, C.RF_CHNLBW, C.RF_CHNLBW_BW_MASK, 0x3)
    # phy_ADC_CLK_8814A runs only on A-cut silicon (this card is not A-cut).
    _spur_nbi(t, channel, rfe_type)


def set_channel_bw(t, channel: int, tx_power_2g: tuple, tx_power_5g: tuple,
                   bb_swing_2g: tuple, bb_swing_5g: tuple,
                   current_band: int = C.BAND_ON_2_4G, rfe_type: int = 1) -> int:
    """Tune to a 2.4 GHz / 5 GHz channel at 20 MHz, then set the per-rate TX power.

    [SRC] phy_SwChnlAndSetBwMode8814A: phy_SwChnl -> phy_SetBwMode ->
    rtw_hal_set_tx_power_level. (IQK, which follows, is a later milestone.) `_phy_sw_chnl`
    band-switches on a 2.4G<->5G crossing (M5a) and selects the channel for either band
    (M5b); TX power is the per-band txagc table (M2e for 2.4 GHz, M5d for 5 GHz).

    ``current_band`` is the vendor's lagging ``current_band_type``; it is returned updated.
    The 2.4 GHz CCK txagc section is written only when it equals BAND_ON_2_4G (the band
    switch committed 2.4 GHz) — so a hop whose band field is still BAND_MAX (post
    init_hw_mlme_ext, before any 5G<->2.4G crossing) skips CCK, matching the wire.
    """
    if channel in C.CHANNELS_2G:
        new_band = _phy_sw_chnl(t, channel, bb_swing_2g, bb_swing_5g, current_band, rfe_type)
        _phy_set_bw_mode_20(t, channel, rfe_type)
        set_tx_power(t, channel, tx_power_2g, write_cck=(new_band == C.BAND_ON_2_4G))  # M2e
        return new_band
    if channel in C.CHANNELS_5G:
        new_band = _phy_sw_chnl(t, channel, bb_swing_2g, bb_swing_5g, current_band, rfe_type)
        _phy_set_bw_mode_20(t, channel, rfe_type)
        set_tx_power_5g(t, channel, tx_power_5g)      # M5d (no CCK on 5 GHz)
        return new_band
    raise NotImplementedError(f"RTL8814AU DKMS port: channel {channel} not supported")


def init_tune(t, channel: int, tx_power_2g: tuple, tx_power_5g: tuple,
              bb_swing_2g: tuple, bb_swing_5g: tuple, rfe_type: int = 1) -> int:
    """Connect-time tune: PHY_ConfigBB + 2.4G band switch + set channel/bw + TX power.

    The explicit 2.4 GHz band switch commits ``current_band_type = BAND_ON_2_4G``, so the
    init tune writes CCK txagc. Returns the committed band."""
    phy_config_bb(t)
    switch_wireless_band_2g(t, bb_swing_2g, rfe_type)  # commits current_band_type = 2.4G
    return set_channel_bw(t, channel, tx_power_2g, tx_power_5g, bb_swing_2g, bb_swing_5g,
                          current_band=C.BAND_ON_2_4G, rfe_type=rfe_type)
