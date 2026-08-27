"""RTL8822BU channel tune — port of `rtw8822b_set_channel` (rtw8822b.c:717).

For monitor-mode RX on 2.4 GHz at 20 MHz we run:
  - rtw8822b_set_channel_bb (2G branch + BW20 ADC settings)
  - rtw_set_channel_mac     (REG_DATA_SC, RFMOD clear, AFE_CTRL1, CCK_CHECK)
  - rtw8822b_set_channel_rf (RF18 band/channel/RFSI/BW write; RF MALSEL;
                             LUTDBG; XTAL toggle)
  - rtw8822b_set_channel_rxdfir (BW20 path)
  - rtw8822b_toggle_igi     (re-arm IGI; reset RXPSEL byte 0)
  - rtw8822b_set_channel_rfe_ifem (for rfe_option=3/5)
  - rtw8822b_set_channel_cca for ifem (CCA thresholds)
"""

from __future__ import annotations

import logging

from wifit3.chips.rtw88_base.registers import (
    REG_USTIME_EDCA,
    REG_USTIME_TSF,
    RTW_CHANNEL_WIDTH_20,
)
from wifit3.chips.rtw88_base.rf_sipi import read_rf, write_rf_masked

from .transport import RTL8822BUTransport

logger = logging.getLogger(__name__)


# --- chip-specific addresses (rtw8822b.h + reg.h) --------------------------
REG_AFE_CTRL1 = 0x0024
BIT_MAC_CLK_SEL = (1 << 20) | (1 << 21)
BIT_SHIFT_MAC_CLK_SEL = 20
MAC_CLK_HW_DEF_80M = 0
MAC_CLK_SPEED = 80

REG_DATA_SC = 0x0483
REG_CCK_CHECK = 0x0454
BIT_CHECK_CCK_EN = 1 << 7

REG_WMAC_TRXPTCL_CTL = 0x0668
BIT_RFMOD = (1 << 7) | (1 << 8)
BIT_RFMOD_40M = 1 << 7
BIT_RFMOD_80M = 1 << 8

# BB-related (rtw8822b.h)
REG_RXPSEL = 0x0808
REG_RXCCAMSK = 0x0814
REG_HTSTFWT = 0x0800
REG_MRC = 0x0850
REG_CLKTRK = 0x0860
REG_ADCCLK = 0x08AC
REG_ADC160 = 0x08C4
REG_RXSB = 0x0A00
REG_ENTXCCK = 0x0A80
REG_TXSF2 = 0x0A24
REG_TXSF6 = 0x0A28
REG_RXDESC = 0x0A2C
REG_ACGG2TBL = 0x0958
REG_ADCINI = 0x0A04
REG_RFEINV = 0x0CBC
REG_RXIGI_A = 0x0C50
REG_RXIGI_B = 0x0E50
REG_AGCTR_A = 0x0C08
REG_AGCTR_B = 0x0E08
REG_TRSW = 0x0CA0
REG_RFESEL0 = 0x0CB0
REG_RFESEL8 = 0x0CB4
REG_CCATH = 0x082C    # the "reg82c" referenced by cca_ccut
REG_REG830 = 0x0830
REG_REG838 = 0x0838

# RF addresses (used by set_channel_rf and config_trx_mode)
RF_MALSEL = 0xBE
RF_LUTDBG = 0xDF
RF_XTALX2 = 0xB8

# RF18 (channel/band/BW/RFSI) — used by set_channel_rf.
RF18_BAND_MASK = (1 << 16) | (1 << 9) | (1 << 8)
RF18_BAND_2G = 0
RF18_BAND_5G = (1 << 16) | (1 << 8)
RF18_CHANNEL_MASK = 0xFF
RF18_RFSI_MASK = (1 << 18) | (1 << 17)
RF18_RFSI_GE_CH80 = 1 << 17
RF18_RFSI_GT_CH144 = 1 << 18
RF18_BW_MASK = (1 << 11) | (1 << 10)
RF18_BW_20M = (1 << 11) | (1 << 10)
RF18_BW_40M = 1 << 11
RF18_BW_80M = 1 << 10

RFBE_MASK = (1 << 17) | (1 << 16) | (1 << 15)

RFREG_MASK = 0xFFFFF
MASKLWORD = 0xFFFF
MASKDWORD = 0xFFFFFFFF
MASKBYTE0 = 0xFF


# --- IFEM CCA constants (rtw8822b.c:366..370) ------------------------------
# Index = [1R_2G, 2R_2G, 1R_5G, 2R_5G]
CCA_IFEM_CCUT_82C = (0x75C97010, 0x75C97010, 0x75C97010, 0x75C97010)
CCA_IFEM_CCUT_830 = (0x79A0EAAA, 0x79A0EAAC, 0x79A0EAAA, 0x79A0EAAA)
CCA_IFEM_CCUT_838 = (0x87765541, 0x87746341, 0x87765541, 0x87746341)


# 5G rfbe lookup tables (rtw8822b.c:490..496).
LOW_BAND = (0x7, 0x6, 0x6, 0x5, 0x0, 0x0, 0x7, 0xFF, 0x6,
            0x5, 0x0, 0x0, 0x7, 0x6, 0x6)
MIDDLE_BAND = (0x6, 0x5, 0x0, 0x0, 0x7, 0x6, 0x6, 0xFF, 0x0,
               0x0, 0x7, 0x6, 0x6, 0x5, 0x0, 0xFF, 0x7, 0x6,
               0x6, 0x5, 0x0, 0x0, 0x7)
HIGH_BAND = (0x5, 0x5, 0x0, 0x7, 0x7, 0x6, 0x5, 0xFF, 0x0,
             0x7, 0x7, 0x6, 0x5, 0x5, 0x0)


# Non-DFS 5 GHz channels (UNII-1 + UNII-3).
CHANNELS_5G_NON_DFS = (36, 40, 44, 48, 149, 153, 157, 161, 165)
# DFS subset (the chip supports tuning to them but rules forbid TX without CAC).
CHANNELS_5G_DFS = (52, 56, 60, 64,
                   100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144)
CHANNELS_5G_ALL = CHANNELS_5G_NON_DFS + CHANNELS_5G_DFS


def _ffs(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def _write32_mask(transport: RTL8822BUTransport, addr: int, mask: int,
                  value: int) -> None:
    if mask == 0xFFFFFFFF:
        transport.write32(addr, value & 0xFFFFFFFF)
        return
    shift = _ffs(mask)
    cur = transport.read32(addr)
    new = (cur & ~mask) | ((value << shift) & mask)
    transport.write32(addr, new & 0xFFFFFFFF)


def is_ch_2g(ch: int) -> bool:
    return ch <= 14


def is_ch_5g_band1(ch: int) -> bool:  # UNII-1
    return 36 <= ch <= 48


def is_ch_5g_band2(ch: int) -> bool:  # UNII-2A
    return 52 <= ch <= 64


def is_ch_5g_band3(ch: int) -> bool:  # UNII-2C / UNII-3 lower
    return 100 <= ch <= 144


def is_ch_5g_band4(ch: int) -> bool:  # UNII-3
    return 149 <= ch <= 177


def is_ch_5g(ch: int) -> bool:
    return (is_ch_5g_band1(ch) or is_ch_5g_band2(ch)
            or is_ch_5g_band3(ch) or is_ch_5g_band4(ch))


# --- rtw_set_channel_mac (mac.c:12..60) -----------------------------------

def set_channel_mac(transport: RTL8822BUTransport, channel: int,
                    bw: int = RTW_CHANNEL_WIDTH_20,
                    primary_ch_idx: int = 0) -> None:
    txsc20 = primary_ch_idx
    txsc40 = 0
    transport.write8(REG_DATA_SC, ((txsc20 & 0xF) | ((txsc40 & 0xF) << 4)) & 0xFF)

    v32 = transport.read32(REG_WMAC_TRXPTCL_CTL) & ~BIT_RFMOD & 0xFFFFFFFF
    if bw == RTW_CHANNEL_WIDTH_20:
        pass  # no extra bit
    elif bw == 1:
        v32 |= BIT_RFMOD_40M
    elif bw == 2:
        v32 |= BIT_RFMOD_80M
    transport.write32(REG_WMAC_TRXPTCL_CTL, v32 & 0xFFFFFFFF)

    # non-8051 path (8822b is not 8051)
    v32 = transport.read32(REG_AFE_CTRL1) & ~BIT_MAC_CLK_SEL & 0xFFFFFFFF
    v32 |= (MAC_CLK_HW_DEF_80M << BIT_SHIFT_MAC_CLK_SEL)
    transport.write32(REG_AFE_CTRL1, v32 & 0xFFFFFFFF)

    transport.write8(REG_USTIME_TSF, MAC_CLK_SPEED)
    transport.write8(REG_USTIME_EDCA, MAC_CLK_SPEED)

    v8 = transport.read8(REG_CCK_CHECK) & ~BIT_CHECK_CCK_EN & 0xFF
    if is_ch_5g(channel):
        v8 |= BIT_CHECK_CCK_EN
    transport.write8(REG_CCK_CHECK, v8)


# --- rtw8822b_set_channel_bb (rtw8822b.c:611..715) ------------------------

def set_channel_bb_2g_20mhz(transport: RTL8822BUTransport, channel: int) -> None:
    _write32_mask(transport, REG_RXPSEL, 1 << 28, 1)
    _write32_mask(transport, REG_CCK_CHECK, 1 << 7, 0)
    _write32_mask(transport, REG_ENTXCCK, 1 << 18, 0)
    _write32_mask(transport, REG_RXCCAMSK, 0x0000FC00, 15)
    _write32_mask(transport, REG_ACGG2TBL, 0x1F, 0)
    _write32_mask(transport, REG_CLKTRK, 0x1FFE0000, 0x96A)

    if channel == 14:
        _write32_mask(transport, REG_TXSF2, MASKDWORD, 0x00006577)
        _write32_mask(transport, REG_TXSF6, MASKLWORD, 0x0000)
    else:
        _write32_mask(transport, REG_TXSF2, MASKDWORD, 0x384F6577)
        _write32_mask(transport, REG_TXSF6, MASKLWORD, 0x1525)

    _write32_mask(transport, REG_RFEINV, 0x300, 2)

    # BW=20 branch
    val32 = transport.read32(REG_ADCCLK)
    val32 &= 0xFFCFFC00
    val32 |= RTW_CHANNEL_WIDTH_20
    transport.write32(REG_ADCCLK, val32 & 0xFFFFFFFF)
    _write32_mask(transport, REG_ADC160, 1 << 30, 1)


# --- rtw8822b_set_channel_rf (rtw8822b.c:498..573) ------------------------

def set_channel_rf(transport: RTL8822BUTransport, channel: int,
                   bw: int = RTW_CHANNEL_WIDTH_20,
                   is_2t2r: bool = True) -> None:
    rf_reg18 = read_rf(transport, 0x18, RFREG_MASK, path="a", udelay_us=20.0)
    rf_reg18 &= ~(RF18_BAND_MASK | RF18_CHANNEL_MASK
                  | RF18_RFSI_MASK | RF18_BW_MASK)

    rf_reg18 |= RF18_BAND_2G if is_ch_2g(channel) else RF18_BAND_5G
    rf_reg18 |= channel & RF18_CHANNEL_MASK
    if channel > 144:
        rf_reg18 |= RF18_RFSI_GT_CH144
    elif channel >= 80:
        rf_reg18 |= RF18_RFSI_GE_CH80

    if bw == RTW_CHANNEL_WIDTH_20:
        rf_reg18 |= RF18_BW_20M
    elif bw == 1:
        rf_reg18 |= RF18_BW_40M
    elif bw == 2:
        rf_reg18 |= RF18_BW_80M

    # rfbe lookup per band (rtw8822b.c:543..552).
    if is_ch_2g(channel):
        rfbe = 0
    elif is_ch_5g_band1(channel) or is_ch_5g_band2(channel):
        rfbe = LOW_BAND[(channel - 36) >> 1]
    elif is_ch_5g_band3(channel):
        rfbe = MIDDLE_BAND[(channel - 100) >> 1]
    elif is_ch_5g_band4(channel):
        rfbe = HIGH_BAND[(channel - 149) >> 1]
    else:
        raise ValueError(f"channel {channel} has no rfbe lookup")

    write_rf_masked(transport, RF_MALSEL, RFBE_MASK, rfbe, path="a")

    if channel == 144:
        write_rf_masked(transport, RF_LUTDBG, 1 << 18, 1, path="a")
    else:
        write_rf_masked(transport, RF_LUTDBG, 1 << 18, 0, path="a")

    write_rf_masked(transport, 0x18, RFREG_MASK, rf_reg18, path="a")
    if is_2t2r:
        write_rf_masked(transport, 0x18, RFREG_MASK, rf_reg18, path="b")

    write_rf_masked(transport, RF_XTALX2, 1 << 19, 0, path="a")
    write_rf_masked(transport, RF_XTALX2, 1 << 19, 1, path="a")


# --- rtw8822b_set_channel_rxdfir (rtw8822b.c:591..609) --------------------

def set_channel_rxdfir(transport: RTL8822BUTransport,
                       bw: int = RTW_CHANNEL_WIDTH_20) -> None:
    REG_ACBB0 = 0x0820   # rtw8822b.h
    REG_ACBBRXFIR = 0x0848
    REG_TXDFIR = 0x080C  # rtw_write32s_mask = REG_TXPSEL in rtw88
    if bw == 1:  # 40 MHz
        _write32_mask(transport, REG_ACBB0, (1 << 29) | (1 << 28), 1)
        _write32_mask(transport, REG_ACBBRXFIR, (1 << 29) | (1 << 28), 0)
        _write32_mask(transport, REG_TXDFIR, 1 << 31, 0)
    elif bw == 2:  # 80 MHz
        _write32_mask(transport, REG_ACBB0, (1 << 29) | (1 << 28), 2)
        _write32_mask(transport, REG_ACBBRXFIR, (1 << 29) | (1 << 28), 1)
        _write32_mask(transport, REG_TXDFIR, 1 << 31, 0)
    else:  # 20 MHz / 10 / 5
        _write32_mask(transport, REG_ACBB0, (1 << 29) | (1 << 28), 2)
        _write32_mask(transport, REG_ACBBRXFIR, (1 << 29) | (1 << 28), 2)
        _write32_mask(transport, REG_TXDFIR, 1 << 31, 1)


# --- rtw8822b_toggle_igi (rtw8822b.c:575..589) ----------------------------

def toggle_igi(transport: RTL8822BUTransport, antenna_rx_paths: int = 0b11) -> None:
    igi_a = transport.read32(REG_RXIGI_A) & 0x7F
    _write32_mask(transport, REG_RXIGI_A, 0x7F, max(0, igi_a - 2))
    _write32_mask(transport, REG_RXIGI_A, 0x7F, igi_a)
    igi_b = transport.read32(REG_RXIGI_B) & 0x7F
    _write32_mask(transport, REG_RXIGI_B, 0x7F, max(0, igi_b - 2))
    _write32_mask(transport, REG_RXIGI_B, 0x7F, igi_b)

    _write32_mask(transport, REG_RXPSEL, MASKBYTE0, 0)
    _write32_mask(transport, REG_RXPSEL, MASKBYTE0,
                  antenna_rx_paths | (antenna_rx_paths << 4))


# --- rtw8822b_set_channel_cca for IFEM (rtw8822b.c:421..495) --------------

def set_channel_cca_ifem(transport: RTL8822BUTransport, channel: int,
                         antenna_rx_paths: int = 0b11) -> None:
    """Apply the IFEM CCA threshold settings.

    `col` selects the {1R/2R, 2G/5G} column of cca_ccut. For 2T2R + 2G that's
    CCUT_IDX_2R_2G = 1.
    """
    is_2r = antenna_rx_paths == 0b11
    if is_ch_2g(channel):
        col = 1 if is_2r else 0
    else:
        col = 3 if is_2r else 2

    transport.write32(REG_CCATH, CCA_IFEM_CCUT_82C[col])
    transport.write32(REG_REG830, CCA_IFEM_CCUT_830[col])
    transport.write32(REG_REG838, CCA_IFEM_CCUT_838[col])


# --- rtw8822b_set_channel_rfe_ifem (rtw8822b.c:319..350) ------------------

def set_channel_rfe_ifem(transport: RTL8822BUTransport, channel: int,
                         antenna_tx_paths: int = 0b11,
                         antenna_rx_paths: int = 0b11) -> None:
    if is_ch_2g(channel):
        _write32_mask(transport, REG_RFESEL0, 0xFFFFFF, 0x745774)
        _write32_mask(transport, REG_RFESEL8, 0xFF00, 0x57)
    else:
        _write32_mask(transport, REG_RFESEL0, 0xFFFFFF, 0x477547)
        _write32_mask(transport, REG_RFESEL8, 0xFF00, 0x75)

    _write32_mask(transport, REG_RFEINV, (1 << 11) | (1 << 10) | 0x3F, 0)

    if is_ch_2g(channel):
        if (antenna_rx_paths == 0b11) or (antenna_tx_paths == 0b11):
            _write32_mask(transport, REG_TRSW, MASKLWORD, 0xA501)
        elif antenna_rx_paths == antenna_tx_paths:
            _write32_mask(transport, REG_TRSW, MASKLWORD, 0xA500)
        else:
            _write32_mask(transport, REG_TRSW, MASKLWORD, 0xA005)
    else:
        _write32_mask(transport, REG_TRSW, MASKLWORD, 0xA5A5)


# --- top-level set_channel (rtw8822b.c:717..736) --------------------------

def set_channel_bb_5g_20mhz(transport: RTL8822BUTransport, channel: int) -> None:
    """5G branch of rtw8822b_set_channel_bb (rtw8822b.c:635..658)."""
    _write32_mask(transport, REG_ENTXCCK, 1 << 18, 1)
    _write32_mask(transport, REG_CCK_CHECK, 1 << 7, 1)
    _write32_mask(transport, REG_RXPSEL, 1 << 28, 0)
    _write32_mask(transport, REG_RXCCAMSK, 0x0000FC00, 34)

    if is_ch_5g_band1(channel) or is_ch_5g_band2(channel):
        _write32_mask(transport, REG_ACGG2TBL, 0x1F, 1)
    elif is_ch_5g_band3(channel):
        _write32_mask(transport, REG_ACGG2TBL, 0x1F, 2)
    elif is_ch_5g_band4(channel):
        _write32_mask(transport, REG_ACGG2TBL, 0x1F, 3)

    if is_ch_5g_band1(channel):
        _write32_mask(transport, REG_CLKTRK, 0x1FFE0000, 0x494)
    elif is_ch_5g_band2(channel):
        _write32_mask(transport, REG_CLKTRK, 0x1FFE0000, 0x453)
    elif 100 <= channel <= 116:
        _write32_mask(transport, REG_CLKTRK, 0x1FFE0000, 0x452)
    elif 118 <= channel <= 177:
        _write32_mask(transport, REG_CLKTRK, 0x1FFE0000, 0x412)

    _write32_mask(transport, 0xCBC, 0x300, 1)

    # BW=20 branch (same as 2G)
    val32 = transport.read32(REG_ADCCLK)
    val32 &= 0xFFCFFC00
    val32 |= RTW_CHANNEL_WIDTH_20
    transport.write32(REG_ADCCLK, val32 & 0xFFFFFFFF)
    _write32_mask(transport, REG_ADC160, 1 << 30, 1)


def set_channel_2g_20mhz(transport: RTL8822BUTransport, channel: int,
                         *, antenna_tx_paths: int = 0b11,
                         antenna_rx_paths: int = 0b11,
                         is_2t2r: bool = True) -> None:
    if not (1 <= channel <= 14):
        raise ValueError(f"2.4 GHz channel must be 1..14, got {channel}")
    logger.debug("set_channel_2g_20mhz ch=%d (2T2R=%s)", channel, is_2t2r)

    set_channel_bb_2g_20mhz(transport, channel)
    set_channel_mac(transport, channel, RTW_CHANNEL_WIDTH_20)
    set_channel_rf(transport, channel, RTW_CHANNEL_WIDTH_20, is_2t2r=is_2t2r)
    set_channel_rxdfir(transport, RTW_CHANNEL_WIDTH_20)
    toggle_igi(transport, antenna_rx_paths)
    set_channel_cca_ifem(transport, channel, antenna_rx_paths)
    set_channel_rfe_ifem(transport, channel,
                         antenna_tx_paths, antenna_rx_paths)


def set_channel_5g_20mhz(transport: RTL8822BUTransport, channel: int,
                         *, antenna_tx_paths: int = 0b11,
                         antenna_rx_paths: int = 0b11,
                         is_2t2r: bool = True) -> None:
    if channel not in CHANNELS_5G_ALL:
        raise ValueError(f"unsupported 5 GHz channel: {channel}")
    logger.debug("set_channel_5g_20mhz ch=%d (2T2R=%s)", channel, is_2t2r)

    set_channel_bb_5g_20mhz(transport, channel)
    set_channel_mac(transport, channel, RTW_CHANNEL_WIDTH_20)
    set_channel_rf(transport, channel, RTW_CHANNEL_WIDTH_20, is_2t2r=is_2t2r)
    set_channel_rxdfir(transport, RTW_CHANNEL_WIDTH_20)
    toggle_igi(transport, antenna_rx_paths)
    set_channel_cca_ifem(transport, channel, antenna_rx_paths)
    set_channel_rfe_ifem(transport, channel,
                         antenna_tx_paths, antenna_rx_paths)


def channel_band_is_2g(channel: int) -> bool:
    return channel <= 14
