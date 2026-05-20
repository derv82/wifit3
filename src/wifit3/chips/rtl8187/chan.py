"""RTL8187L channel tune.

Port of ``rtl8225_rf_set_channel`` (rtl8225.c:986-1002). The function
is variant-agnostic — kernel dispatches the TX-power update to BCD vs
z2 vs z2_b based on ``priv->rf->init`` — and we do the same via the
:class:`~wifit3.chips.rtl8187.rtl8225.RfVariant` enum.

After the per-variant TX-power refresh, the synthesizer is retuned by
writing the channel-specific word from the shared ``rtl8225_chan`` table
to RF register 7, then sleeping 10 ms for the PLL to lock.

This replaces the 156-instruction-per-channel replay sequences from the
old driver — a single tune costs ~12 register writes (~3 if the chip
is on the 8051 fast-path).
"""
from __future__ import annotations

import logging
import time

from .rtl8225 import (
    RfVariant,
    rtl8225_chan,
    rtl8225_rf_set_tx_power,
    rtl8225_write,
    rtl8225z2_rf_set_tx_power,
)
from .transport import RTL8187Transport

logger = logging.getLogger(__name__)

# Channels 1..14 (2.4 GHz only). Channel 14 is JP-only and uses a
# different CCK power table — supported by the kernel set_chan but kept
# off the default hop list (see RTL8187Driver.SUPPORTED_CHANNELS).
VALID_CHANNELS = tuple(range(1, 15))


def set_channel(
    t: RTL8187Transport,
    asic_rev: int,
    variant: RfVariant,
    channel: int,
) -> None:
    """Retune the synthesizer to ``channel`` (1..14).

    Raises ValueError for out-of-range channels. Kernel does no bounds
    check (it trusts ieee80211_frequency_to_channel) but we surface
    bad inputs explicitly.
    """
    if channel not in VALID_CHANNELS:
        raise ValueError(f"RTL8187L: channel {channel} out of range (1..14)")

    # 1) Per-variant TX-power refresh. Uses the table appropriate to
    #    the silicon revision.
    if variant is RfVariant.RTL8225Z2:
        rtl8225z2_rf_set_tx_power(t, channel)
    else:
        rtl8225_rf_set_tx_power(t, channel)

    # 2) Write the synthesizer word — single RF register write.
    rtl8225_write(t, 0x7, rtl8225_chan[channel - 1], asic_rev)

    # 3) PLL settle.
    time.sleep(0.010)

    logger.debug(
        "set_channel: ch=%d, RF7=0x%03x, variant=%s",
        channel, rtl8225_chan[channel - 1], variant.value,
    )
