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

from wifit3.chips.rtw88_base.rf_sipi import (
    RFREG_MASK,
    read_rf as _shared_read_rf,
    write_rf_masked as _shared_write_rf_masked,
)

from .constants import (
    BIT_RFMOD,
    REG_ADC160,
    REG_ADCCLK,
    REG_CLKTRK,
    REG_DATA_SC,
    REG_L1PKTH,
    REG_WMAC_TRXPTCL_CTL,
    RF18_BAND_MASK,
    RF18_BW_MASK,
    RF18_CHANNEL_MASK,
    RF18_RFSI_MASK,
    RF_CFGCH,
)
from .transport import RTL8821AUTransport

logger = logging.getLogger(__name__)


def read_rf(transport: RTL8821AUTransport, addr: int, mask: int = RFREG_MASK) -> int:
    """SIPI read of RF reg `addr` (path A) — 8821A unconditional 20us udelay."""
    return _shared_read_rf(transport, addr, mask, path="a", udelay_us=20.0)


def write_rf_masked(transport: RTL8821AUTransport, addr: int, mask: int, data: int) -> None:
    """SIPI write to RF reg `addr` (path A) — 8821A unconditional 13us udelay."""
    _shared_write_rf_masked(transport, addr, mask, data, path="a", udelay_us=13.0)


# ---------------------------------------------------------------------------
# Sub-helpers — only the 8821A 1T1R 2.4 GHz code paths
# ---------------------------------------------------------------------------

def _lookup_fc_area(channel: int) -> int:
    """Mirrors the channel→fc_area switch in rtw88xxa_switch_channel."""
    if 36 <= channel <= 48:
        return 0x494
    if 50 <= channel <= 64:
        return 0x453
    if 100 <= channel <= 116:
        return 0x452
    if channel >= 118:
        return 0x412
    return 0x96A   # 2.4 GHz default


def _lookup_rf_mod_ag(channel: int) -> int:
    """Mirrors the channel→rf_mod_ag switch in rtw88xxa_switch_channel."""
    if 36 <= channel <= 64:
        return 0x101
    if 100 <= channel <= 140:
        return 0x301
    if channel > 140:
        return 0x501
    return 0x000   # 2.4 GHz default


def _switch_channel(transport: RTL8821AUTransport, channel: int) -> None:
    """rtw88xxa.c:1324, unified for 2.4 GHz and 5 GHz.

    Writes the centre-frequency area (REG_CLKTRK) then two SIPI writes
    to RF18 (band/RFSI bits, then channel index).
    """
    fc_area = _lookup_fc_area(channel)
    transport.write32_mask(REG_CLKTRK, 0x1FFE0000, fc_area)
    rf_mod_ag = _lookup_rf_mod_ag(channel)
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
    if not (1 <= channel <= 14):
        raise ValueError(f"2.4 GHz channel must be 1..14, got {channel}")
    logger.debug("set_channel_2g_20mhz: ch=%d primary_idx=%d", channel, primary_chan_idx)
    _switch_channel(transport, channel)
    _post_set_bw_mode_20mhz(transport, primary_chan_idx)
    _set_channel_rf_20mhz(transport)


# Valid 5 GHz channels (rtw88 supports the full UNII range; we expose the
# subset that doesn't require DFS clearance from a regulator).
CHANNELS_5G_NON_DFS = (
    36, 40, 44, 48,                 # UNII-1 (5.18 – 5.24 GHz)
    149, 153, 157, 161, 165,        # UNII-3 (5.745 – 5.825 GHz)
)
CHANNELS_5G_DFS = (
    52, 56, 60, 64,                 # UNII-2A
    100, 104, 108, 112, 116, 120, 124, 128,
    132, 136, 140, 144,             # UNII-2C
)
CHANNELS_5G_ALL = CHANNELS_5G_NON_DFS + CHANNELS_5G_DFS


def set_channel_5g_20mhz(
    transport: RTL8821AUTransport,
    channel: int,
    *,
    primary_chan_idx: int = 0,
) -> None:
    """Tune to a 5 GHz channel at 20 MHz bandwidth.

    Caller must have already band-switched to 5G via
    :func:`phy.switch_band_5g_20mhz`. This just runs the
    channel/bw-specific writes.
    """
    if channel not in CHANNELS_5G_ALL:
        raise ValueError(f"unsupported 5 GHz channel: {channel}")
    logger.debug("set_channel_5g_20mhz: ch=%d primary_idx=%d", channel, primary_chan_idx)
    _switch_channel(transport, channel)
    _post_set_bw_mode_20mhz(transport, primary_chan_idx)
    _set_channel_rf_20mhz(transport)


def channel_band_is_2g(channel: int) -> bool:
    return channel <= 14
