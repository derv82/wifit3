"""RTL8187L channel tune.

Port of ``rtl8187_config`` (dev.c:1155-1179) and its inner ``rtl8225_rf_set_channel``
(rtl8225.c:986-1002). A channel change is NOT just the synth write: the kernel brackets the
whole tune in a TX_CONF MAC-loopback window (its comment: TX during a channel change "causes
problems and the card will stop work until next reset"), then rewrites the ATIM/beacon
interval registers afterwards. ``config_channel`` reproduces that whole sequence; ``set_chan``
is the inner per-variant TX-power refresh + RF7 synth write.

The per-channel TX power comes from the EEPROM (``TxPower``, read at probe) — not a stub — so
each hop writes the calibrated TX_GAIN_CCK/OFDM the kernel writes.
"""
from __future__ import annotations

import logging
import time

from .constants import (
    REG_ATIM_WND,
    REG_ATIMTR_INTERVAL,
    REG_BEACON_INTERVAL,
    REG_BEACON_INTERVAL_TIME,
    REG_TX_CONF,
    TX_CONF_LOOPBACK_MAC,
)
from .rtl8225 import (
    RfVariant,
    TxPower,
    rtl8225_chan,
    rtl8225_write,
    set_tx_power,
)
from .transport import RTL8187Transport

logger = logging.getLogger(__name__)

# Channels 1..14 (2.4 GHz only). Channel 14 is JP-only and uses a
# different CCK power table — supported by the kernel set_chan but kept
# off the default hop list (see RTL8187Driver.SUPPORTED_CHANNELS).
VALID_CHANNELS = tuple(range(1, 15))


def set_chan(
    t: RTL8187Transport,
    asic_rev: int,
    variant: RfVariant,
    channel: int,
    power: TxPower,
) -> None:
    """rtl8225_rf_set_channel: per-variant TX-power refresh + the single RF7 synth write."""
    set_tx_power(t, variant, channel, power)
    rtl8225_write(t, 0x7, rtl8225_chan[channel - 1], asic_rev)
    time.sleep(0.010)  # PLL settle


def config_channel(
    t: RTL8187Transport,
    asic_rev: int,
    variant: RfVariant,
    channel: int,
    power: TxPower,
) -> None:
    """Retune to ``channel`` (1..14), full ``rtl8187_config`` sequence.

    Raises ValueError for out-of-range channels. The TX_CONF base is read back from the
    chip (it carries read-only HWVER bits), so the loopback set/restore is a read-modify-
    write the replay serves — never a hardcoded value.
    """
    if channel not in VALID_CHANNELS:
        raise ValueError(f"RTL8187L: channel {channel} out of range (1..14)")

    # Enable MAC loopback so no TX leaks out mid-tune, then retune, then restore TX_CONF.
    reg = t.read32(REG_TX_CONF)
    t.write32(REG_TX_CONF, reg | TX_CONF_LOOPBACK_MAC)
    set_chan(t, asic_rev, variant, channel, power)
    time.sleep(0.010)
    t.write32(REG_TX_CONF, reg)

    # ATIM / beacon interval defaults (dev.c:1173-1176).
    t.write16(REG_ATIM_WND, 2)
    t.write16(REG_ATIMTR_INTERVAL, 100)
    t.write16(REG_BEACON_INTERVAL, 100)
    t.write16(REG_BEACON_INTERVAL_TIME, 100)

    logger.debug(
        "config_channel: ch=%d, RF7=0x%03x, variant=%s",
        channel, rtl8225_chan[channel - 1], variant.value,
    )
