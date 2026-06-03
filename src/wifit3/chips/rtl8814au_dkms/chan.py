"""RTL8814AU channel tune (M2d) — vendor faithful port, 20 MHz primary only.

Mirrors the hal_init tail [SRC usb_halinit.c:1229-1237]:
    PHY_ConfigBB_8814A           enable OFDM + CCK
    PHY_SwitchWirelessBand8814A  switch to the 2.4 GHz band
    rtw_hal_set_chnl_bw(.., CHANNEL_WIDTH_20, ..) -> phy_SwChnl8814A (channel) +
                                 phy_SetBwMode8814A (20 MHz) + spur-cal reset

Per the 20-MHz-only scope, the 40/80 MHz width math is omitted. TX power
(rtw_hal_set_tx_power_level, the 0x1998 loop) and IQK follow in the vendor flow but
are TX/cal concerns deferred to a later milestone. 5G band tune is also deferred.

RF register writes/reads go through the memory-mapped interface in ``rf.py``.
Verified byte-for-byte; [WIRE] cap1 frames 13695-13855 (channel 1).
"""
from __future__ import annotations

from . import constants as C
from .bb import _set_reg_masked as _bb32
from .rf import set_rf_masked
from .txpower import set_tx_power

_RF_PATHS = ("a", "b", "c", "d")


def _bb8_clear_set(t, addr: int, bit: int, set_bit: bool) -> None:
    v = t.read8(addr)
    t.write8(addr, (v | bit) if set_bit else (v & ~bit))


def phy_config_bb(t) -> None:
    """[SRC] PHY_ConfigBB_8814A — enable OFDM + CCK (rOFDMCCKEN[29:28] = 0x3)."""
    _bb32(t, C.rOFDMCCKEN, C.bOFDMEN | C.bCCKEN, 0x3)


def _set_rfe_reg_2g(t) -> None:
    """[SRC] PHY_SetRFEReg8814A(FALSE, 2.4G), rfe_type=1 — RFE pinmux + inv."""
    for reg in C.RFE_PINMUX:
        t.write32(reg, C.RFE_PINMUX_VAL)
    _bb32(t, C.REG_RFE_INV, 0x0FF00000, 0x77)


def set_rfe_reg_init(t, rfe_type: int) -> None:
    """[SRC] PHY_SetRFEReg8814A(bInit=TRUE) — RFE control enable + GPIO pinmux.

    Run once from the hal_init turn-on block. Enables the RFE control field
    (0x1994[3:0]=0xf) and drives the GPIO antenna-select pins: rfe 1/2 set
    0x42[23:20]=0xf (|0xf0), rfe 0 sets 0x42[23:22]=2b'11 (|0xc0).
    """
    _bb32(t, C.RFE_8814_REG, 0xF, 0xF)
    gpio_bits = 0xF0 if rfe_type in (1, 2) else 0xC0
    v = t.read8(C.REG_GPIO_IO_SEL_8814A)
    t.write8(C.REG_GPIO_IO_SEL_8814A, v | gpio_bits)


def _set_bb_swing_2g(t, bb_swing: tuple) -> None:
    """[SRC] phy_SetBBSwingByBand_8814A(2.4G) — per-path TxScale[31:21].

    ``bb_swing`` is the per-path 11-bit TxScale value decoded from efuse 0xC6
    (``efuse._parse_bb_swing_2g``); on an unburned fuse every path is the 0 dB
    default (0x200), which is what this card reads.
    """
    for reg, val in zip(C.TXSCALE, bb_swing):
        _bb32(t, reg, C.BBSWING_MASK, val)


def _set_bw_reg_adc_agc_20(t) -> None:
    """[SRC] phy_SetBwRegAdc_8814A / phy_SetBwRegAgc_8814A for CHANNEL_WIDTH_20."""
    _bb32(t, C.rRFMOD, 0x3, 0x0)              # ADC: 0x8ac[1:0] = 0
    _bb32(t, C.rAGC_table_Jaguar, 0xF000, 0x6)  # AGC: 0x82c[15:12] = 6


def switch_wireless_band_2g(t, bb_swing: tuple) -> None:
    """[SRC] PHY_SwitchWirelessBand8814A(BAND_ON_2_4G), 20 MHz, mp_mode=0."""
    _bb8_clear_set(t, C.REG_SYS_CFG3_2, 0x01, False)   # gate CCK/OFDM clock off
    _bb32(t, C.rAGC_table_Jaguar2, 0x1F, 0x0)          # 2.4G AGC table select
    _set_rfe_reg_2g(t)
    _bb32(t, C.rTxPath, 0xF0, 0x2)
    _bb32(t, C.rCCK_RX, 0x0F000000, 0x5)
    _bb32(t, C.rOFDMCCKEN, C.bOFDMEN | C.bCCKEN, 0x3)
    t.write8(C.REG_CCK_CHECK, 0x0)
    _bb32(t, C.REG_A80, 1 << 18, 0x0)
    _set_bb_swing_2g(t, bb_swing)
    _set_bw_reg_adc_agc_20(t)
    _bb8_clear_set(t, C.REG_SYS_CFG3_2, 0x01, True)     # gate CCK/OFDM clock on


def _phy_sw_chnl(t, channel: int) -> None:
    """[SRC] phy_SwChnl8814A — 2.4 GHz channel select (per-path RF + CCK DFIR)."""
    t.read8(C.REG_CCK_CHECK)                  # phy_SwBand: band detect (2.4G, no switch)
    _bb32(t, C.rFc_area, 0x1FFE0000, 0x96A)   # center-freq area (ch <= 36)
    for path in _RF_PATHS:                    # RF_MOD_AG = 0 for 2.4G -> value = channel
        set_rf_masked(t, path, C.RF_CHNLBW, C.RF_CHNLBW_CH_MASK, channel)
    # 2.4G CCK TX DFIR
    if 1 <= channel <= 11:
        f2, dbg = 0x090E1317, 0x00000204
    else:                                     # channels 12-13
        f2, dbg = 0x090E1217, 0x00000305
    t.write32(C.rCCK0_TxFilter1, 0x1A1B0030)
    t.write32(C.rCCK0_TxFilter2, f2)
    t.write32(C.rCCK0_DebugPort, dbg)


# [SRC] phydm_set_nbi_reg nbi_128[] (tone_idx x10) — 8814A 20/40 MHz uses the FFT-128
# table. reg_idx = (first index whose entry exceeds tone_idx) + 1, written to 0x87c[19:14].
_NBI_128 = (25, 55, 85, 115, 135, 155, 185, 205, 225, 245, 265, 285, 305, 335, 355,
            375, 395, 415, 435, 455, 485, 505, 525, 555, 585, 615, 635)
# [SRC] phydm_spur_nbi_setting_8814a (rfe 0/1/6/7): the 2.4 GHz spur interferers — a
# per-channel NBI notch on ch 4-8 (2440 MHz) and ch 14 (2480 MHz); all others disable NBI.
_SPUR_INTF_2G = {4: 2440, 5: 2440, 6: 2440, 7: 2440, 8: 2440, 14: 2480}


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


def _spur_nbi_2g(t, channel: int) -> None:
    """[SRC] phy_SpurCalibration_8814A (CSI reset) + phydm_spur_nbi_setting_8814a (NBI).

    2.4 GHz channels 4-8 (and 14) carry a spur the vendor notches with a per-channel NBI
    tap at 0x87c[19:14] + NBI-enable 0x87c[13]; every other channel disables NBI (the
    [19:13]=0x7E reset). The CSI mask/fix-mask reset is common to all channels. Byte-diffed
    per channel by scripts/rtl8814au_dkms/verify_channels.py.
    """
    # phy_SpurCalibration_8814A: reset the NBI tap + CSI mask/fix-mask. Every channel.
    _bb32(t, C.rNBI_Setting, 0x000FE000, 0xFC >> 1)
    _bb32(t, C.rCSI_Mask_Setting1, 0x1, 0x0)
    for reg in C.rCSI_FIX_MASK:
        t.write32(reg, 0x0)
    # phydm_spur_nbi_setting_8814a: a spur channel sets the per-channel notch tap then
    # enables NBI; every other channel just disables NBI.
    f_intf = _SPUR_INTF_2G.get(channel)
    if f_intf is None:
        _bb32(t, C.rNBI_Setting, C.NBI_EN_BIT, 0x0)
    else:
        _bb32(t, C.rNBI_Setting, 0x000FC000, _nbi_reg_idx(channel, f_intf))
        _bb32(t, C.rNBI_Setting, C.NBI_EN_BIT, 0x1)


def _phy_set_bw_mode_20(t, channel: int) -> None:
    """[SRC] phy_SetBwMode8814A — CHANNEL_WIDTH_20."""
    v = t.read16(C.REG_TRXPTCL_CTL)           # MAC bw: clear BIT7|BIT8
    t.write16(C.REG_TRXPTCL_CTL, v & ~((1 << 7) | (1 << 8)))
    t.write8(C.REG_DATA_SC, 0x0)              # secondary channel = 0
    _set_bw_reg_adc_agc_20(t)
    for path in _RF_PATHS:                    # RF bw: 0x18[11:10] = 3
        set_rf_masked(t, path, C.RF_CHNLBW, C.RF_CHNLBW_BW_MASK, 0x3)
    # phy_ADC_CLK_8814A runs only on A-cut silicon (this card is not A-cut).
    _spur_nbi_2g(t, channel)


def set_channel_bw(t, channel: int, tx_power: tuple) -> None:
    """Tune to a 2.4 GHz channel at 20 MHz, then set the per-rate TX power.

    [SRC] phy_SwChnlAndSetBwMode8814A: phy_SwChnl -> phy_SetBwMode ->
    rtw_hal_set_tx_power_level. (IQK, which follows, is a later milestone.)
    """
    if not 1 <= channel <= 14:
        raise NotImplementedError(f"RTL8814AU DKMS port: 5G channel {channel} is M2d+")
    _phy_sw_chnl(t, channel)
    _phy_set_bw_mode_20(t, channel)
    set_tx_power(t, channel, tx_power)   # M2e


def init_tune(t, channel: int, tx_power: tuple, bb_swing: tuple) -> None:
    """Connect-time tune: PHY_ConfigBB + 2.4G band switch + set channel/bw + TX power."""
    phy_config_bb(t)
    switch_wireless_band_2g(t, bb_swing)
    set_channel_bw(t, channel, tx_power)
