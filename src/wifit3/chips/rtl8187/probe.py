"""Faithful RTL8187L probe — mirrors ``rtl8187_probe``'s on-the-wire sequence (dev.c).

Bit-bangs the 93cx6 EEPROM for the permanent MAC, per-channel TX power and ``txpwr_base``,
reads ``asic_rev`` and the HWVER, then probes the RF variant — in the exact order the
kernel emits on the bus, so the acceptance gate reproduces it single-cursor. Nothing is
hardcoded: the per-channel TX power read here is what feeds every ``set_tx_power`` (the RF
init's channel-1 refresh and each channel tune). [[feedback_verify_pcap_fully_diffable]]

[SRC] ``data_dumps/rtl818x-source-v6.18/rtl8187/dev.c`` (rtl8187_probe, lines ~1490-1648).
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    EEPROM_CMD_CONFIG,
    EEPROM_CMD_NORMAL,
    EEPROM_MAC_ADDR,
    EEPROM_TXPWR_BASE,
    EEPROM_TXPWR_CHAN_1,
    EEPROM_TXPWR_CHAN_4,
    EEPROM_TXPWR_CHAN_6,
    REG_EEPROM_CMD,
    REG_MAGIC_ASIC_REV,
    REG_PGSELECT,
)
from .eeprom import Eeprom93cx6, eeprom_width
from .mac import ChipVariant, detect_chip_variant
from .rfkill import is_radio_enabled
from .rtl8225 import RfSetup, TxPower, detect_rf
from .transport import RTL8187Transport

# LED customer-ID EEPROM word (read for rtl8187_leds_init; we don't drive the LEDs, but the
# read is on the wire when CONFIG_RTL8187_LEDS is built in, as it is on the Kali kernel).
EEPROM_LED_CUSTID = 0x3F


@dataclass(frozen=True)
class ProbeResult:
    mac: bytes
    chip: ChipVariant
    setup: RfSetup          # asic_rev + RF variant (for set_channel dispatch)
    power: TxPower          # per-channel hw_value + txpwr_base (for set_tx_power)


def probe(t: RTL8187Transport) -> ProbeResult:
    """Run the cold probe and return everything bring-up + channel tune need.

    Order mirrors dev.c exactly: EEPROM-width pick → open config window → MAC + channel
    1..10 TX power + base → asic_rev → close window → HWVER → channel 11..14 TX power →
    detect_rf → LED custid read → rfkill poll. Phases of the channel-power read straddle
    the asic_rev/HWVER reads because the kernel does (the 11..14 words live in the
    ``!is_rtl8187b`` tail at dev.c:1586)."""
    width = eeprom_width(t)                                  # RX_CONF bit 6 (dev.c:1496)

    t.write8(REG_EEPROM_CMD, EEPROM_CMD_CONFIG)              # open analog/EEPROM window
    ee = Eeprom93cx6(t, width)

    mac_words = ee.multiread(EEPROM_MAC_ADDR, 3)             # 3 LE words → 6 MAC bytes
    mac = bytes(b for w in mac_words for b in (w & 0xFF, w >> 8))

    hw: list[int] = []
    for i in range(3):                                      # channels 1..6 (3 words)
        w = ee.read(EEPROM_TXPWR_CHAN_1 + i)
        hw += [w & 0xFF, w >> 8]
    for i in range(2):                                      # channels 7..10 (2 words)
        w = ee.read(EEPROM_TXPWR_CHAN_4 + i)
        hw += [w & 0xFF, w >> 8]
    base = ee.read(EEPROM_TXPWR_BASE)                       # u16 txpwr_base

    reg = t.read8(REG_PGSELECT) & ~1                        # asic_rev: 0=bit-bang, 1=8051
    t.write8(REG_PGSELECT, reg | 1)
    asic_rev = t.read8(REG_MAGIC_ASIC_REV) & 0x3
    t.write8(REG_PGSELECT, reg & 0xFF)

    t.write8(REG_EEPROM_CMD, EEPROM_CMD_NORMAL)             # close window

    chip = detect_chip_variant(t)                          # HWVER read (TX_CONF[27:25])

    for i in range(2):                                     # channels 11..14 (2 words)
        w = ee.read(EEPROM_TXPWR_CHAN_6 + i)
        hw += [w & 0xFF, w >> 8]

    variant = detect_rf(t, asic_rev)                       # RF reg 0/8/9 SPI probe

    ee.read(EEPROM_LED_CUSTID)                              # LED customer-ID (read only)
    is_radio_enabled(t)                                    # rfkill_init poll

    return ProbeResult(
        mac=mac,
        chip=chip,
        setup=RfSetup(asic_rev=asic_rev, variant=variant),
        power=TxPower(hw_value=tuple(hw), base=base),
    )
