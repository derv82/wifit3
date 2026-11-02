"""RTL8187L / RTL8187B probe — mirrors ``rtl8187_probe``'s on-the-wire sequence (dev.c).

Bit-bangs the 93cx6 EEPROM for the permanent MAC, per-channel TX power and ``txpwr_base``,
reads ``asic_rev`` and the HWVER, then probes the RF variant — in the exact order the
kernel emits on the bus, so the acceptance gate reproduces it single-cursor. Nothing is
hardcoded: the per-channel TX power read here is what feeds every ``set_tx_power`` (the RF
init's channel-1 refresh and each channel tune).

[SRC] ``driver_sources/rtl818x-source-v6.18/rtl8187/dev.c`` (rtl8187_probe, lines ~1490-1648).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .constants import (
    EEPROM_CMD_CONFIG,
    EEPROM_CMD_NORMAL,
    EEPROM_MAC_ADDR,
    EEPROM_SELECT_GPIO,
    EEPROM_TXPWR_BASE,
    EEPROM_TXPWR_CHAN_1,
    EEPROM_TXPWR_CHAN_4,
    EEPROM_TXPWR_CHAN_6,
    REG_EEPROM_CMD,
    REG_MAGIC_ASIC_REV,
    REG_PGSELECT,
    RFKILL_MASK_8187_89_97,
    RFKILL_MASK_8198,
)
from .eeprom import Eeprom93cx6, eeprom_width
from .mac import ChipVariant, detect_chip_variant, detect_rtl8187b_hw_rev
from .rfkill import is_radio_enabled
from .rtl8225 import RfSetup, TxPower, detect_rf
from .transport import RTL8187Transport

logger = logging.getLogger(__name__)

# LED customer-ID EEPROM word (read for rtl8187_leds_init; we don't drive the LEDs, but the
# read is on the wire when CONFIG_RTL8187_LEDS is built in, as it is on the Kali kernel).
EEPROM_LED_CUSTID = 0x3F


@dataclass(frozen=True)
class ProbeResult:
    mac: bytes
    chip: ChipVariant
    setup: RfSetup          # asic_rev + RF variant (for set_channel dispatch)
    power: TxPower          # per-channel hw_value + txpwr_base (for set_tx_power)
    rfkill_mask: int        # priv->rfkill_mask (dev.c:1607-1612)
    rfkill_off: bool        # priv->rfkill_off: True when the radio switch is ON (rfkill.c:35)


def is_valid_ether_addr(addr: bytes) -> bool:
    """[SRC] include/linux/etherdevice.h: not multicast and not all zeros."""
    return not (addr[0] & 0x01) and any(addr)


def eth_random_addr() -> bytes:
    """[SRC] include/linux/etherdevice.h: random, unicast, locally administered."""
    addr = bytearray(os.urandom(6))
    addr[0] &= 0xFE
    addr[0] |= 0x02
    return bytes(addr)


def probe(t: RTL8187Transport, product_id: int, is_rtl8187b: bool) -> ProbeResult:
    """Run the cold probe and return everything bring-up + channel tune need.

    ``is_rtl8187b`` is the id's driver_info == DEVICE_RTL8187B; ``product_id`` is idProduct.
    The channel 11..14 power words straddle the asic_rev/HWVER reads because the kernel's do."""
    width = eeprom_width(t)                                  # RX_CONF bit 6 (dev.c:1496)

    t.write8(REG_EEPROM_CMD, EEPROM_CMD_CONFIG)              # open analog/EEPROM window
    ee = Eeprom93cx6(t, width)

    mac_words = ee.multiread(EEPROM_MAC_ADDR, 3)             # 3 LE words → 6 MAC bytes
    mac = bytes(b for w in mac_words for b in (w & 0xFF, w >> 8))
    if not is_valid_ether_addr(mac):                         # dev.c:1506-1510
        logger.warning("rtl8187: Invalid hwaddr! Using randomly generated MAC address")
        mac = eth_random_addr()

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

    if not is_rtl8187b:
        chip = detect_chip_variant(t)                      # HWVER read (TX_CONF[27:25])
    else:
        chip = detect_rtl8187b_hw_rev(t)                   # 0xFFE1 read

    if not chip.is_rtl8187b:
        for i in range(2):                                 # channels 11..14 (2 words)
            w = ee.read(EEPROM_TXPWR_CHAN_6 + i)
            hw += [w & 0xFF, w >> 8]
    else:                                                  # dev.c:1594-1605
        hw.append(ee.read(EEPROM_TXPWR_CHAN_6) & 0xFF)     # channel 11
        hw.append(ee.read(0x0A) & 0xFF)                    # channel 12
        w = ee.read(0x1C)                                  # channels 13..14
        hw += [w & 0xFF, w >> 8]

    rfkill_mask = RFKILL_MASK_8187_89_97                   # dev.c:1607-1612
    if product_id == 0x8197 or product_id == 0x8198:
        if ee.read(EEPROM_SELECT_GPIO) & 0xFF00:
            rfkill_mask = RFKILL_MASK_8198

    if not is_rtl8187b and chip.is_rtl8187b:               # dev.c:1619-1621
        logger.info("rtl8187: inconsistency between id with OEM info!")

    variant = detect_rf(t, asic_rev, chip.is_rtl8187b)     # RF reg 0/8/9 SPI probe (L only)
    logger.info("rtl8187: %s V%d + %s, rfkill mask %d",
                chip.name, asic_rev, variant.value, rfkill_mask)

    ee.read(EEPROM_LED_CUSTID)                              # LED customer-ID (read only)
    rfkill_off = is_radio_enabled(t, rfkill_mask)          # rtl8187_rfkill_init

    return ProbeResult(
        mac=mac,
        chip=chip,
        setup=RfSetup(asic_rev=asic_rev, variant=variant, hw_rev=chip.hw_rev),
        power=TxPower(hw_value=tuple(hw), base=base),
        rfkill_mask=rfkill_mask,
        rfkill_off=rfkill_off,
    )
