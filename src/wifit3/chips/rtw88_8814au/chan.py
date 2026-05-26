"""RTL8814AU channel tune (20 MHz) — port of rtw8814a_set_channel.

Covers the bring-up-relevant path for monitor-mode RX at 20 MHz:

    set_channel
      ├── switch_band (only on 2G<->5G change): rfe pinmux + CCK/TX/RX psel +
      │     bw_reg_adc/agc, bracketed by BB_RSTB clear/set
      ├── switch_channel: per-path RF_CFGCH (channel + band mod) + CLKTRK + AGC
      ├── cck_tx_dfir: 2.4G CCK TX filter
      └── set_bw_mode (20 MHz): bw_reg_mac/adc/agc + DATA_SC + bw_rf

Deferred (not needed for 20 MHz monitor RX; documented in RTL8814AU.md):
  - set_channel_bb_swing / pwrtrack_init : TX power scaling (M6)
  - rtw8814a_adc_clk                     : A-cut only (no-op on our B-cut)
  - rtw8814a_spur_calibration            : per-spur-channel NBI/CSI notch
  - 40/80 MHz bandwidth paths            : monitor is 20 MHz

References: rtw8814a.c rtw8814a_set_channel + sub-functions.
"""

from __future__ import annotations

import logging

from . import constants as C
from . import rf
from .transport import RTL8814AUTransport

logger = logging.getLogger(__name__)

SUPPORTED_CHANNELS_2G = list(range(1, 14))
SUPPORTED_CHANNELS_5G = [36, 40, 44, 48, 149, 153, 157, 161, 165]


def _set_rfe_reg_24g(transport: RTL8814AUTransport, rfe: int) -> None:
    """rtw8814a_set_rfe_reg_24g."""
    if rfe == 2:
        pinmux = (0x72707270, 0x72707270, 0x72707270, 0x77707770)
        selsw0 = 0x72
    elif rfe == 1:
        pinmux = (0x77777777, 0x77777777, 0x77777777, 0x77777777)
        selsw0 = 0x77
    else:  # 0 / default — kernel leaves PINMUX_D untouched
        pinmux = (0x77777777, 0x77777777, 0x77777777, None)
        selsw0 = 0x77
    for reg, val in zip(
        (C.REG_RFE_PINMUX_A, C.REG_RFE_PINMUX_B, C.REG_RFE_PINMUX_C, C.REG_RFE_PINMUX_D),
        pinmux,
    ):
        if val is not None:
            transport.write32(reg, val)
    transport.write32_mask(C.REG_RFE_INVSEL_D, C.BIT_RFE_SELSW0_D, selsw0)


def _set_rfe_reg_5g(transport: RTL8814AUTransport, rfe: int) -> None:
    """rtw8814a_set_rfe_reg_5g."""
    if rfe == 2:
        pinmux = (0x37173717, 0x37173717, 0x37173717, 0x77177717)
        selsw0 = 0x37
    elif rfe == 1:
        pinmux = (0x33173317, 0x33173317, 0x33173317, 0x77177717)
        selsw0 = 0x33
    else:
        pinmux = (0x54775477, 0x54775477, 0x54775477, 0x54775477)
        selsw0 = 0x54
    for reg, val in zip(
        (C.REG_RFE_PINMUX_A, C.REG_RFE_PINMUX_B, C.REG_RFE_PINMUX_C, C.REG_RFE_PINMUX_D),
        pinmux,
    ):
        transport.write32(reg, val)
    transport.write32_mask(C.REG_RFE_INVSEL_D, C.BIT_RFE_SELSW0_D, selsw0)


def _set_bw_reg_adc(transport: RTL8814AUTransport, bw: int) -> None:
    adc = {C.RTW_CHANNEL_WIDTH_20: 0, C.RTW_CHANNEL_WIDTH_40: 1,
           C.RTW_CHANNEL_WIDTH_80: 2}.get(bw, 0)
    transport.write32_mask(C.REG_ADCCLK, 0x3, adc)


def _set_bw_reg_agc(transport: RTL8814AUTransport, new_band: int, bw: int) -> None:
    if bw == C.RTW_CHANNEL_WIDTH_20:
        agc = 6
    elif bw == C.RTW_CHANNEL_WIDTH_40:
        agc = 8 if new_band == C.RTW_BAND_5G else 7
    elif bw == C.RTW_CHANNEL_WIDTH_80:
        agc = 3
    else:
        agc = 7
    transport.write32_mask(C.REG_CCASEL, 0xF000, agc)


def _set_bw_reg_mac(transport: RTL8814AUTransport, bw: int) -> None:
    val16 = transport.read16(C.REG_WMAC_TRXPTCL_CTL) & ~C.BIT_RFMOD
    if bw == C.RTW_CHANNEL_WIDTH_80:
        val16 |= C.BIT_RFMOD_80M
    elif bw == C.RTW_CHANNEL_WIDTH_40:
        val16 |= C.BIT_RFMOD_40M
    transport.write16(C.REG_WMAC_TRXPTCL_CTL, val16 & 0xFFFF)


def _set_bw_rf(transport: RTL8814AUTransport, bw: int, rf_path_num: int) -> None:
    bwbits = {C.RTW_CHANNEL_WIDTH_40: 1, C.RTW_CHANNEL_WIDTH_80: 0}.get(bw, 3)
    for path in range(rf_path_num):
        rf.write_rf(transport, path, C.RF_CFGCH, C.RF18_BW_MASK, bwbits)


def _switch_band(transport: RTL8814AUTransport, new_band: int, bw: int,
                 rfe: int) -> None:
    """rtw8814a_switch_band (bb_swing deferred to M6)."""
    from wifit3.chips.rtw88_base.registers import BIT_FEN_BB_RSTB

    # Gate CCK/OFDM clock off while reconfiguring.
    transport.write8_clr(C.REG_SYS_CFG3_8814A + 2, BIT_FEN_BB_RSTB & 0xFF)

    if new_band == C.RTW_BAND_2G:
        transport.write32_mask(C.REG_AGC_TABLE, 0x1F, 0)
        _set_rfe_reg_24g(transport, rfe)
        transport.write32_mask(C.REG_TXPSEL, 0xF0, 0x2)
        transport.write32_mask(C.REG_CCK_RX, 0x0F000000, 0x5)
        transport.write32_mask(C.REG_RXPSEL, C.BIT_RX_PSEL_RST, 0x3)
        transport.write8(C.REG_CCK_CHECK, 0)
        transport.write32_mask(C.REG_CCK_TX_EN, 1 << 18, 0)
    else:
        transport.write8(C.REG_CCK_CHECK, C.BIT_CHECK_CCK_EN)
        transport.write32_mask(C.REG_CCK_TX_EN, 1 << 18, 1)
        _set_rfe_reg_5g(transport, rfe)
        transport.write32_mask(C.REG_TXPSEL, 0xF0, 0x0)
        transport.write32_mask(C.REG_CCK_RX, 0x0F000000, 0xF)
        transport.write32_mask(C.REG_RXPSEL, C.BIT_RX_PSEL_RST, 0x2)

    # bb_swing (TX scaling) deferred to M6.
    _set_bw_reg_adc(transport, bw)
    _set_bw_reg_agc(transport, new_band, bw)

    transport.write8_set(C.REG_SYS_CFG3_8814A + 2, BIT_FEN_BB_RSTB & 0xFF)


def _switch_channel(transport: RTL8814AUTransport, channel: int,
                    rf_path_num: int) -> None:
    """rtw8814a_switch_channel — per-path RF_CFGCH + CLKTRK fc_area + AGC."""
    if 36 <= channel <= 48:
        fc_area = 0x494
    elif 50 <= channel <= 64:
        fc_area = 0x453
    elif 100 <= channel <= 116:
        fc_area = 0x452
    elif channel >= 118:
        fc_area = 0x412
    else:
        fc_area = 0x96A
    transport.write32_mask(C.REG_CLKTRK, 0x1FFE0000, fc_area)

    for path in range(rf_path_num):
        if 36 <= channel <= 64:
            rf_mod_ag = 0x101
        elif 100 <= channel <= 140:
            rf_mod_ag = 0x301
        elif channel > 140:
            rf_mod_ag = 0x501
        else:
            rf_mod_ag = 0x000
        cfgch = (rf_mod_ag << 8) | channel
        rf.write_rf(transport, path, C.RF_CFGCH,
                    C.RF18_RFSI_MASK | C.RF18_BAND_MASK | C.RF18_CHANNEL_MASK,
                    cfgch)

    if 36 <= channel <= 64:
        transport.write32_mask(C.REG_AGC_TABLE, 0x1F, 1)
    elif 100 <= channel <= 144:
        transport.write32_mask(C.REG_AGC_TABLE, 0x1F, 2)
    elif channel >= 149:
        transport.write32_mask(C.REG_AGC_TABLE, 0x1F, 3)


def _cck_tx_dfir(transport: RTL8814AUTransport, channel: int) -> None:
    """rtw8814a_24g_cck_tx_dfir — 2.4G CCK TX filter."""
    if 1 <= channel <= 11:
        f1, f2, dbg = 0x1A1B0030, 0x090E1317, 0x00000204
    elif 12 <= channel <= 13:
        f1, f2, dbg = 0x1A1B0030, 0x090E1217, 0x00000305
    elif channel == 14:
        f1, f2, dbg = 0x1A1B0030, 0x00000E17, 0x00000000
    else:
        return
    transport.write32(C.REG_CCK0_TX_FILTER1, f1)
    transport.write32(C.REG_CCK0_TX_FILTER2, f2)
    transport.write32(C.REG_CCK0_DEBUG_PORT, dbg)


def _set_bw_mode(transport: RTL8814AUTransport, new_band: int, channel: int,
                 bw: int, primary_chan_idx: int, rf_path_num: int) -> None:
    """rtw8814a_set_bw_mode (20 MHz path; adc_clk + spur_cal deferred)."""
    _set_bw_reg_mac(transport, bw)

    if bw != C.RTW_CHANNEL_WIDTH_20:
        raise NotImplementedError("8814au tune supports 20 MHz only for now")
    txsc = 0  # 20 MHz primary: txsc20 = primary_chan_idx = 0, txsc40 = 0
    transport.write8(C.REG_DATA_SC, txsc)

    _set_bw_reg_adc(transport, bw)
    _set_bw_reg_agc(transport, new_band, bw)
    _set_bw_rf(transport, bw, rf_path_num)
    # adc_clk: A-cut only (no-op on B-cut). spur_calibration: deferred.


def set_channel(transport: RTL8814AUTransport, channel: int, *,
                bw: int = C.RTW_CHANNEL_WIDTH_20, primary_chan_idx: int = 0,
                rfe_option: int = 1, rf_path_num: int = 4,
                force_band: bool = False) -> None:
    """Tune to `channel` at 20 MHz. Switches band on a 2G<->5G crossing.

    `force_band=True` runs switch_band unconditionally — used on the first tune
    after phy_set_param to set the rfe pinmux + band regs (we skip the kernel's
    init_rfe_reg in phy_set_param, so the first tune must establish them).
    """
    old_band = (C.RTW_BAND_5G
                if transport.read8(C.REG_CCK_CHECK) & C.BIT_CHECK_CCK_EN
                else C.RTW_BAND_2G)
    new_band = C.RTW_BAND_5G if channel > 14 else C.RTW_BAND_2G

    if new_band != old_band or force_band:
        _switch_band(transport, new_band, bw, rfe_option)

    _switch_channel(transport, channel, rf_path_num)
    _cck_tx_dfir(transport, channel)
    _set_bw_mode(transport, new_band, channel, bw, primary_chan_idx, rf_path_num)
    logger.info("tuned to channel %d (band %s, 20 MHz)",
                channel, "5G" if new_band == C.RTW_BAND_5G else "2G")
