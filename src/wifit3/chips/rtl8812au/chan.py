"""RTL8812AU channel tune for 2.4 GHz / 5 GHz, 20 MHz bandwidth.

Port of `rtw88xxa_set_channel` and its helpers, specialised for the
8812A 2T2R path. Major differences from the 8821A port:

* `_switch_channel` writes RF18 (CFGCH) on BOTH paths A and B.
* `_set_channel_rf_20mhz` likewise writes RF18 bandwidth on both paths.
* `rtw8812a_phy_fix_spur` is called between the rf_mod_ag write and the
  channel-index write — fixes ADC clock for channels 13/14.
* `_post_set_bw_mode_20mhz` uses `L1PKTH = 7` for 2T2R (vs 8821A's 8).
* RF reads need a `set_cca` dance on 8812A non-CUT-C (rtw88xxa.c:1266).

Reference (rtw88-source-v6.18):
    rtw88xxa.c:1292  rtw8812a_phy_fix_spur
    rtw88xxa.c:1324  rtw88xxa_switch_channel
    rtw88xxa.c:1389  rtw88xxa_post_set_bw_mode
    rtw88xxa.c:1467  rtw88xxa_set_channel_rf
    rtw88xxa.c:1489  rtw88xxa_set_channel
    phy.c:1029       rtw_phy_write_rf_reg_sipi
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from wifit3.chips.rtw88_base.rf_sipi import (
    write_rf_masked as _shared_write_rf_masked,
)

from .constants import (
    BIT_RFMOD,
    REG_ADC160,
    REG_ADCCLK,
    REG_CCA2ND,
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
from .transport import RTL8812AUTransport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 8812a SIPI write with set_cca dance
# ---------------------------------------------------------------------------

@contextmanager
def _cca_frozen(transport: RTL8812AUTransport):
    """Freeze CCA on REG_CCA2ND BIT(3) for the duration of the block.

    Mirrors the `set_cca` dance in `rtw88xxa_phy_read_rf` (rtw88xxa.c:1266),
    needed for reliable RF reads on 8812A. The kernel restricts this to
    non-CUT-C, but we apply it unconditionally — on CUT-C the extra
    register toggle is a harmless no-op.
    """
    transport.write32_set(REG_CCA2ND, 1 << 3)
    try:
        yield
    finally:
        transport.write32_clr(REG_CCA2ND, 1 << 3)


def _write_rf18(transport: RTL8812AUTransport, mask: int, value: int, *, path: str) -> None:
    """Read-modify-write RF18 (CFGCH) on the given path.

    Wrapper around the family-shared SIPI write that internally read-backs
    RF18 to merge bits. Caller is responsible for being inside a
    `_cca_frozen` block if mask < RFREG_MASK.
    """
    _shared_write_rf_masked(transport, RF_CFGCH, mask, value, path=path, udelay_us=13.0)


# ---------------------------------------------------------------------------
# Sub-helpers (8812A, 2.4 GHz, 20 MHz)
# ---------------------------------------------------------------------------

def _lookup_fc_area(channel: int) -> int:
    """Channel → centre-frequency area code (rtw88xxa.c:1330..1346)."""
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
    """Channel → RF18 rf_mod_ag value (rtw88xxa.c:1351..1364)."""
    if 36 <= channel <= 64:
        return 0x101
    if 100 <= channel <= 140:
        return 0x301
    if channel > 140:
        return 0x501
    return 0x000   # 2.4 GHz default


def _phy_fix_spur_non_cutc(transport: RTL8812AUTransport, channel: int, bw: int) -> None:
    """Non-CUT-C branch of rtw8812a_phy_fix_spur (rtw88xxa.c:1313..1321).

    The full CUT-C branch handles 40/80 MHz spur fixes too; we only ship
    20 MHz for M3 so the non-CUT-C path suffices. (For 8812a on AWUS036ACH
    we don't yet know the cut version — without EFUSE we assume non-CUT-C
    which is conservatively correct.)
    """
    from .constants import RTW_CHANNEL_WIDTH_20  # local import to avoid cycles
    if bw == RTW_CHANNEL_WIDTH_20 and channel in (13, 14):
        transport.write32_mask(REG_ADCCLK, 0x300, 0x3)
    elif channel <= 14:
        transport.write32_mask(REG_ADCCLK, 0x300, 0x2)


def _switch_channel(transport: RTL8812AUTransport, channel: int, bw: int) -> None:
    """Port of rtw88xxa_switch_channel (rtw88xxa.c:1324) for 8812A 2T2R.

    Writes the centre-frequency area then iterates over BOTH RF paths,
    each doing rf_mod_ag + spur-fix + channel-index writes.
    """
    fc_area = _lookup_fc_area(channel)
    transport.write32_mask(REG_CLKTRK, 0x1FFE0000, fc_area)

    rf_mod_ag = _lookup_rf_mod_ag(channel)

    for path in ("a", "b"):
        with _cca_frozen(transport):
            _write_rf18(transport, RF18_RFSI_MASK | RF18_BAND_MASK, rf_mod_ag, path=path)
        _phy_fix_spur_non_cutc(transport, channel, bw)
        with _cca_frozen(transport):
            _write_rf18(transport, RF18_CHANNEL_MASK, channel, path=path)


def _set_reg_bw_20mhz(transport: RTL8812AUTransport) -> None:
    """Clear BIT_RFMOD in REG_WMAC_TRXPTCL_CTL (20 MHz)."""
    val16 = transport.read16(REG_WMAC_TRXPTCL_CTL)
    val16 &= ~BIT_RFMOD & 0xFFFF
    transport.write16(REG_WMAC_TRXPTCL_CTL, val16)


def _post_set_bw_mode_20mhz(transport: RTL8812AUTransport, primary_chan_idx: int) -> None:
    """20 MHz branch of rtw88xxa_post_set_bw_mode (rtw88xxa.c:1389).

    For 8812A 2T2R: L1PKTH = 7 (vs 8821A's 8 for 1T1R).
    """
    _set_reg_bw_20mhz(transport)

    # 20 MHz: BIT_TXSC_20M(p)|BIT_TXSC_40M(0). primary_chan_idx=0 for DONT_CARE.
    txsc = (primary_chan_idx & 0xF)
    transport.write8(REG_DATA_SC, txsc)

    transport.write32_mask(REG_ADCCLK, 0x003003C3, 0x00300200)
    transport.write32_mask(REG_ADC160, 1 << 30, 0)
    # 2T2R → L1PKTH=7
    transport.write32_mask(REG_L1PKTH, 0x03C00000, 7)


def _set_channel_rf_20mhz(transport: RTL8812AUTransport) -> None:
    """20 MHz branch of rtw88xxa_set_channel_rf (rtw88xxa.c:1467) for 2T2R.

    Writes RF18 BW field on BOTH paths.
    """
    for path in ("a", "b"):
        with _cca_frozen(transport):
            _write_rf18(transport, RF18_BW_MASK, 3, path=path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def set_channel_2g_20mhz(
    transport: RTL8812AUTransport,
    channel: int,
    *,
    primary_chan_idx: int = 0,
) -> None:
    """Tune to a 2.4 GHz channel at 20 MHz bandwidth (2T2R).

    Caller must have already band-switched to 2G via
    :func:`phy.switch_band_2g_20mhz`.
    """
    from .constants import RTW_CHANNEL_WIDTH_20
    if not (1 <= channel <= 14):
        raise ValueError(f"2.4 GHz channel must be 1..14, got {channel}")
    logger.debug("set_channel_2g_20mhz: ch=%d primary_idx=%d", channel, primary_chan_idx)
    _switch_channel(transport, channel, RTW_CHANNEL_WIDTH_20)
    _post_set_bw_mode_20mhz(transport, primary_chan_idx)
    _set_channel_rf_20mhz(transport)


# 5 GHz channels — non-DFS subset exposed by default. DFS channels need
# regulator clearance and are off-limits without dynamic-frequency-selection
# infrastructure that wifit3 doesn't implement.
CHANNELS_5G_NON_DFS = (36, 40, 44, 48, 149, 153, 157, 161, 165)
CHANNELS_5G_DFS = (
    52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
)
CHANNELS_5G_ALL = CHANNELS_5G_NON_DFS + CHANNELS_5G_DFS


def channel_band_is_2g(channel: int) -> bool:
    return channel <= 14


def set_channel_5g_20mhz(
    transport: RTL8812AUTransport,
    channel: int,
    *,
    primary_chan_idx: int = 0,
) -> None:
    """Tune to a 5 GHz channel at 20 MHz bandwidth (2T2R).

    Caller must have already band-switched to 5G via
    :func:`phy.switch_band_5g_20mhz`. This just does the channel/bw RF
    writes — _switch_channel already understands 5G channel ranges via
    the fc_area + rf_mod_ag lookups.
    """
    from .constants import RTW_CHANNEL_WIDTH_20
    if channel not in CHANNELS_5G_ALL:
        raise ValueError(f"unsupported 5 GHz channel: {channel}")
    logger.debug("set_channel_5g_20mhz: ch=%d primary_idx=%d", channel, primary_chan_idx)
    _switch_channel(transport, channel, RTW_CHANNEL_WIDTH_20)
    _post_set_bw_mode_20mhz(transport, primary_chan_idx)
    _set_channel_rf_20mhz(transport)
