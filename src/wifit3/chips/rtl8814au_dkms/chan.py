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


def _set_bb_swing_2g(t) -> None:
    """[SRC] phy_SetBBSwingByBand_8814A(2.4G) — per-path TxScale[31:21].

    The swing index comes from efuse TxBBSwing; this card uses the 0 dB default
    (0x200). The per-board TxBBSwing decode is deferred with the TX-power port.
    """
    for reg in C.TXSCALE:
        _bb32(t, reg, C.BBSWING_MASK, C.BBSWING_DEFAULT)


def _set_bw_reg_adc_agc_20(t) -> None:
    """[SRC] phy_SetBwRegAdc_8814A / phy_SetBwRegAgc_8814A for CHANNEL_WIDTH_20."""
    _bb32(t, C.rRFMOD, 0x3, 0x0)              # ADC: 0x8ac[1:0] = 0
    _bb32(t, C.rAGC_table_Jaguar, 0xF000, 0x6)  # AGC: 0x82c[15:12] = 6


def switch_wireless_band_2g(t) -> None:
    """[SRC] PHY_SwitchWirelessBand8814A(BAND_ON_2_4G), 20 MHz, mp_mode=0."""
    _bb8_clear_set(t, C.REG_SYS_CFG3_2, 0x01, False)   # gate CCK/OFDM clock off
    _bb32(t, C.rAGC_table_Jaguar2, 0x1F, 0x0)          # 2.4G AGC table select
    _set_rfe_reg_2g(t)
    _bb32(t, C.rTxPath, 0xF0, 0x2)
    _bb32(t, C.rCCK_RX, 0x0F000000, 0x5)
    _bb32(t, C.rOFDMCCKEN, C.bOFDMEN | C.bCCKEN, 0x3)
    t.write8(C.REG_CCK_CHECK, 0x0)
    _bb32(t, C.REG_A80, 1 << 18, 0x0)
    _set_bb_swing_2g(t)
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


def _spur_cal_reset(t) -> None:
    """[SRC] phy_SpurCalibration_8814A — 2.4 GHz has no spur, so reset NBI/CSI.

    Then phydm_spur_nbi_setting_8814a disables NBI for non-spur channels
    (phydm_nbi_enable -> clear 0x87c[13]).
    """
    _bb32(t, C.rNBI_Setting, 0x000FE000, 0xFC >> 1)
    _bb32(t, C.rCSI_Mask_Setting1, 0x1, 0x0)
    for reg in C.rCSI_FIX_MASK:
        t.write32(reg, 0x0)
    _bb32(t, C.rNBI_Setting, C.NBI_EN_BIT, 0x0)


def _phy_set_bw_mode_20(t) -> None:
    """[SRC] phy_SetBwMode8814A — CHANNEL_WIDTH_20."""
    v = t.read16(C.REG_TRXPTCL_CTL)           # MAC bw: clear BIT7|BIT8
    t.write16(C.REG_TRXPTCL_CTL, v & ~((1 << 7) | (1 << 8)))
    t.write8(C.REG_DATA_SC, 0x0)              # secondary channel = 0
    _set_bw_reg_adc_agc_20(t)
    for path in _RF_PATHS:                    # RF bw: 0x18[11:10] = 3
        set_rf_masked(t, path, C.RF_CHNLBW, C.RF_CHNLBW_BW_MASK, 0x3)
    # phy_ADC_CLK_8814A runs only on A-cut silicon (this card is not A-cut).
    _spur_cal_reset(t)


def set_channel_bw(t, channel: int) -> None:
    """Tune to a 2.4 GHz channel at 20 MHz (phy_SwChnl + phy_SetBwMode)."""
    if not 1 <= channel <= 14:
        raise NotImplementedError(f"RTL8814AU DKMS port: 5G channel {channel} is M2d+")
    _phy_sw_chnl(t, channel)
    _phy_set_bw_mode_20(t)


def init_tune(t, channel: int) -> None:
    """Connect-time tune: PHY_ConfigBB + 2.4G band switch + set channel/bw."""
    phy_config_bb(t)
    switch_wireless_band_2g(t)
    set_channel_bw(t, channel)
