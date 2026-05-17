"""RTL8821AU channel tune for 2.4 GHz.

Port of `rtw88xxa_set_channel` (rtw88xxa.c:1489) plus its helpers
`switch_channel` (line 1324), `post_set_bw_mode` (line 1389), and
`set_channel_rf` (line 1467) — specialised for the 8821A 1T1R path,
channels 1..13, 20 MHz.

The kernel does read-modify-write on RF register 0x18 (CFGCH) several
times per channel switch (band bits / channel bits / BW bits live at
different bit positions of the same RF reg). We mirror that via
:func:`read_rf` / :func:`write_rf_masked`, which together implement the
SIPI read + masked write described in `rtw_phy_write_rf_reg_sipi`
(phy.c:1029) and `rtw88xxa_phy_read_rf` (rtw88xxa.c:1245).

References:
    phy.c:1029       rtw_phy_write_rf_reg_sipi
    rtw88xxa.c:1245  rtw88xxa_phy_read_rf
    rtw88xxa.c:1324  rtw88xxa_switch_channel
    rtw88xxa.c:1389  rtw88xxa_post_set_bw_mode
    rtw88xxa.c:1467  rtw88xxa_set_channel_rf
    rtw88xxa.c:1489  rtw88xxa_set_channel
"""
from __future__ import annotations

import logging
import time

from .constants import (
    BIT_RFMOD,
    REG_3WIRE_SWA,
    REG_ADC160,
    REG_ADCCLK,
    REG_CLKTRK,
    REG_DATA_SC,
    REG_HSSI_READ,
    REG_L1PKTH,
    REG_LSSI_WRITE_A,
    REG_PI_READ_A,
    REG_SI_READ_A,
    REG_WMAC_TRXPTCL_CTL,
    RF18_BAND_MASK,
    RF18_BW_MASK,
    RF18_CHANNEL_MASK,
    RF18_RFSI_MASK,
    RF_CFGCH,
    RFREG_MASK,
    RTW_CHANNEL_WIDTH_20,
)
from .transport import RTL8821AUTransport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RF SIPI read / write (path A)
# ---------------------------------------------------------------------------

def _ffs(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def read_rf(transport: RTL8821AUTransport, addr: int, mask: int = RFREG_MASK) -> int:
    """SIPI read of RF reg `addr` (path A), shifted into `mask`'s position.

    Mirrors rtw88xxa_phy_read_rf (rtw88xxa.c:1245) for the 8821A 1T1R
    path. Always udelays 20us (8821A unconditionally does this regardless
    of cut).
    """
    addr &= 0xFF
    pi_mode = (transport.read32(REG_3WIRE_SWA) >> 2) & 1   # BIT(2)
    transport.write32_mask(REG_HSSI_READ, 0xFF, addr)
    time.sleep(20e-6)
    read_reg = REG_PI_READ_A if pi_mode else REG_SI_READ_A
    cur = transport.read32(read_reg)
    shift = _ffs(mask)
    return (cur & mask) >> shift


def write_rf_masked(transport: RTL8821AUTransport, addr: int, mask: int, data: int) -> None:
    """SIPI write to RF reg `addr` (path A) with mask-aware RMW.

    Mirrors rtw_phy_write_rf_reg_sipi (phy.c:1029). If `mask == RFREG_MASK`
    the kernel skips the read-back; otherwise it reads, merges, writes.
    """
    addr &= 0xFF
    mask &= RFREG_MASK

    if mask != RFREG_MASK:
        old = read_rf(transport, addr, RFREG_MASK)
        shift = _ffs(mask)
        data = (old & ~mask) | ((data << shift) & mask)

    data_and_addr = ((addr << 20) | (data & RFREG_MASK)) & 0x0FFFFFFF
    transport.write32(REG_LSSI_WRITE_A, data_and_addr)
    time.sleep(13e-6)


# ---------------------------------------------------------------------------
# Sub-helpers — only the 8821A 1T1R 2.4 GHz code paths
# ---------------------------------------------------------------------------

def _switch_channel(transport: RTL8821AUTransport, channel: int) -> None:
    """rtw88xxa.c:1324, 2.4 GHz branch.

    For ch 1..14 the default `fc_area = 0x96a` and `rf_mod_ag = 0x000`.
    """
    if not (1 <= channel <= 14):
        raise ValueError(f"2.4 GHz channel must be 1..14, got {channel}")

    fc_area = 0x96A
    transport.write32_mask(REG_CLKTRK, 0x1FFE0000, fc_area)

    rf_mod_ag = 0x000
    write_rf_masked(transport, RF_CFGCH, RF18_RFSI_MASK | RF18_BAND_MASK, rf_mod_ag)
    write_rf_masked(transport, RF_CFGCH, RF18_CHANNEL_MASK, channel)


def _set_reg_bw_20mhz(transport: RTL8821AUTransport) -> None:
    """Clear BIT_RFMOD in REG_WMAC_TRXPTCL_CTL (20 MHz)."""
    val16 = transport.read16(REG_WMAC_TRXPTCL_CTL)
    val16 &= ~BIT_RFMOD & 0xFFFF
    transport.write16(REG_WMAC_TRXPTCL_CTL, val16)


def _post_set_bw_mode_20mhz(transport: RTL8821AUTransport, primary_chan_idx: int) -> None:
    """20 MHz branch of rtw88xxa_post_set_bw_mode (rtw88xxa.c:1389)."""
    _set_reg_bw_20mhz(transport)

    # txsc encoding: BIT_TXSC_20M(p) | BIT_TXSC_40M(0); for 20MHz primary
    # is RTW_SC_DONT_CARE=0 from mac80211, so REG_DATA_SC = 0.
    txsc = ((primary_chan_idx & 0xF) << 0) | (0 << 4)
    transport.write8(REG_DATA_SC, txsc)

    transport.write32_mask(REG_ADCCLK, 0x003003C3, 0x00300200)
    transport.write32_mask(REG_ADC160, 1 << 30, 0)
    # 1T1R for 8821A → L1PKTH value = 8
    transport.write32_mask(REG_L1PKTH, 0x03C00000, 8)


def _set_channel_rf_20mhz(transport: RTL8821AUTransport) -> None:
    """20 MHz branch of rtw88xxa_set_channel_rf (rtw88xxa.c:1467)."""
    write_rf_masked(transport, RF_CFGCH, RF18_BW_MASK, 3)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def set_channel_2g_20mhz(
    transport: RTL8821AUTransport,
    channel: int,
    *,
    primary_chan_idx: int = 0,
) -> None:
    """Tune to a 2.4 GHz channel at 20 MHz bandwidth.

    Mirrors rtw88xxa_set_channel (rtw88xxa.c:1489) for the case
    channel ∈ 1..14, bw = RTW_CHANNEL_WIDTH_20, 8821A 1T1R, band-switch
    not needed (we assume :func:`phy.switch_band_2g_20mhz` already ran).

    Args:
        primary_chan_idx: 0 = DONT_CARE (mac80211 default for 20MHz).
    """
    logger.info("set_channel_2g_20mhz: ch=%d primary_idx=%d", channel, primary_chan_idx)
    _switch_channel(transport, channel)
    _post_set_bw_mode_20mhz(transport, primary_chan_idx)
    _set_channel_rf_20mhz(transport)
