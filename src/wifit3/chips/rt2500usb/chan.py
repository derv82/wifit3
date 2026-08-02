"""rt2500usb RF register access + channel tuning.

The RF synthesizer is programmed serially through PHY_CSR9 (low 16 bits)
and PHY_CSR10 (high bits + RF_BUSY trigger + bit count). RF registers are
**write-only** — there is no read path. Port of rt2500usb.c:
  * rt2500usb_rf_write       (179-206)
  * rt2500usb_config_channel (582-611)
  * rf_vals_bg_{2522,2523,2524,2525,2525e} + rf_vals_5222 (1511-1660)

The RF chip is a per-card runtime discriminator (EEPROM_ANTENNA_RF_TYPE); all
six kernel rf_vals tables are ported so the driver tunes whichever synth the
EEPROM reports. The RT2500USB.md reference unit is RF2525E; the other five are
ported 1:1 but not hardware-verified (see config_channel's untested-variant log).

Verified against driver_captures/captures_rt2500usb/capture-2 channel-1 set
(frames 1237-1271): the RF2525E "half-band first" RF[2] write
(0x000008aa → PHY_CSR9=0x08aa, PHY_CSR10=0x9400) and the full
RF[1..4] sequence match the kernel 1:1. (The capture used power_level
20, so its RF[3] = 0x00062911; the value is a runtime parameter.)
"""
from __future__ import annotations

import logging

from .bbp import bbp_read, bbp_write
from .constants import (
    ANTENNA_A,
    ANTENNA_HW_DIVERSITY,
    ANTENNA_SW_DIVERSITY,
    BBP_R2_TX_ANTENNA,
    BBP_R2_TX_IQ_FLIP,
    BBP_R14_RX_ANTENNA,
    BBP_R14_RX_IQ_FLIP,
    DEFAULT_TXPOWER,
    EEPROM_ANTENNA_RX_DEFAULT,
    EEPROM_ANTENNA_TX_DEFAULT,
    MAX_TXPOWER,
    MIN_TXPOWER,
    PHY_CSR5,
    PHY_CSR5_CCK,
    PHY_CSR5_CCK_FLIP,
    PHY_CSR6,
    PHY_CSR6_OFDM,
    PHY_CSR6_OFDM_FLIP,
    PHY_CSR9,
    PHY_CSR9_RF_VALUE,
    PHY_CSR10,
    PHY_CSR10_RF_BUSY,
    PHY_CSR10_RF_IF_SELECT,
    PHY_CSR10_RF_NUMBER_OF_BITS,
    PHY_CSR10_RF_VALUE,
    RF2522,
    RF2523,
    RF2524,
    RF2525,
    RF2525E,
    RF3_TXPOWER,
    RF5222,
)
from .transport import RT2500USBTransport, get_field16, set_field16

logger = logging.getLogger(__name__)

# rf_channel tables: channel -> (rf1, rf2, rf3, rf4). rf3 carries the
# TXPOWER field, overwritten per-tune. rf4 == 0 means "no RF[4] write".
# One table per RF chip, keyed by EEPROM_ANTENNA_RF_TYPE; all six are ported
# 1:1 from the kernel rf_vals_* arrays so the driver tunes whatever synth the
# card's EEPROM reports. Only the 2.4 GHz channels (1-14) are carried — the
# driver is 2.4 GHz-only (SUPPORTED_CHANNELS), so RF5222's 5 GHz rows
# (rt2500usb.c:1632-1659) are intentionally omitted.

# rt2500usb.c:1511-1526 (RF2522). rf4 == 0: RF2522 issues no RF[4] write.
RF_VALS_2522: dict[int, tuple[int, int, int, int]] = {
    1:  (0x00002050, 0x000c1fda, 0x00000101, 0x00000000),
    2:  (0x00002050, 0x000c1fee, 0x00000101, 0x00000000),
    3:  (0x00002050, 0x000c2002, 0x00000101, 0x00000000),
    4:  (0x00002050, 0x000c2016, 0x00000101, 0x00000000),
    5:  (0x00002050, 0x000c202a, 0x00000101, 0x00000000),
    6:  (0x00002050, 0x000c203e, 0x00000101, 0x00000000),
    7:  (0x00002050, 0x000c2052, 0x00000101, 0x00000000),
    8:  (0x00002050, 0x000c2066, 0x00000101, 0x00000000),
    9:  (0x00002050, 0x000c207a, 0x00000101, 0x00000000),
    10: (0x00002050, 0x000c208e, 0x00000101, 0x00000000),
    11: (0x00002050, 0x000c20a2, 0x00000101, 0x00000000),
    12: (0x00002050, 0x000c20b6, 0x00000101, 0x00000000),
    13: (0x00002050, 0x000c20ca, 0x00000101, 0x00000000),
    14: (0x00002050, 0x000c20fa, 0x00000101, 0x00000000),
}

# rt2500usb.c:1532-1547 (RF2523).
RF_VALS_2523: dict[int, tuple[int, int, int, int]] = {
    1:  (0x00022010, 0x00000c9e, 0x000e0111, 0x00000a1b),
    2:  (0x00022010, 0x00000ca2, 0x000e0111, 0x00000a1b),
    3:  (0x00022010, 0x00000ca6, 0x000e0111, 0x00000a1b),
    4:  (0x00022010, 0x00000caa, 0x000e0111, 0x00000a1b),
    5:  (0x00022010, 0x00000cae, 0x000e0111, 0x00000a1b),
    6:  (0x00022010, 0x00000cb2, 0x000e0111, 0x00000a1b),
    7:  (0x00022010, 0x00000cb6, 0x000e0111, 0x00000a1b),
    8:  (0x00022010, 0x00000cba, 0x000e0111, 0x00000a1b),
    9:  (0x00022010, 0x00000cbe, 0x000e0111, 0x00000a1b),
    10: (0x00022010, 0x00000d02, 0x000e0111, 0x00000a1b),
    11: (0x00022010, 0x00000d06, 0x000e0111, 0x00000a1b),
    12: (0x00022010, 0x00000d0a, 0x000e0111, 0x00000a1b),
    13: (0x00022010, 0x00000d0e, 0x000e0111, 0x00000a1b),
    14: (0x00022010, 0x00000d1a, 0x000e0111, 0x00000a03),
}

# rt2500usb.c:1553-1568 (RF2524).
RF_VALS_2524: dict[int, tuple[int, int, int, int]] = {
    1:  (0x00032020, 0x00000c9e, 0x00000101, 0x00000a1b),
    2:  (0x00032020, 0x00000ca2, 0x00000101, 0x00000a1b),
    3:  (0x00032020, 0x00000ca6, 0x00000101, 0x00000a1b),
    4:  (0x00032020, 0x00000caa, 0x00000101, 0x00000a1b),
    5:  (0x00032020, 0x00000cae, 0x00000101, 0x00000a1b),
    6:  (0x00032020, 0x00000cb2, 0x00000101, 0x00000a1b),
    7:  (0x00032020, 0x00000cb6, 0x00000101, 0x00000a1b),
    8:  (0x00032020, 0x00000cba, 0x00000101, 0x00000a1b),
    9:  (0x00032020, 0x00000cbe, 0x00000101, 0x00000a1b),
    10: (0x00032020, 0x00000d02, 0x00000101, 0x00000a1b),
    11: (0x00032020, 0x00000d06, 0x00000101, 0x00000a1b),
    12: (0x00032020, 0x00000d0a, 0x00000101, 0x00000a1b),
    13: (0x00032020, 0x00000d0e, 0x00000101, 0x00000a1b),
    14: (0x00032020, 0x00000d1a, 0x00000101, 0x00000a03),
}

# rt2500usb.c:1574-1589 (RF2525) and 1595-1610 (RF2525E).
RF_VALS_2525: dict[int, tuple[int, int, int, int]] = {
    1:  (0x00022020, 0x00080c9e, 0x00060111, 0x00000a1b),
    2:  (0x00022020, 0x00080ca2, 0x00060111, 0x00000a1b),
    3:  (0x00022020, 0x00080ca6, 0x00060111, 0x00000a1b),
    4:  (0x00022020, 0x00080caa, 0x00060111, 0x00000a1b),
    5:  (0x00022020, 0x00080cae, 0x00060111, 0x00000a1b),
    6:  (0x00022020, 0x00080cb2, 0x00060111, 0x00000a1b),
    7:  (0x00022020, 0x00080cb6, 0x00060111, 0x00000a1b),
    8:  (0x00022020, 0x00080cba, 0x00060111, 0x00000a1b),
    9:  (0x00022020, 0x00080cbe, 0x00060111, 0x00000a1b),
    10: (0x00022020, 0x00080d02, 0x00060111, 0x00000a1b),
    11: (0x00022020, 0x00080d06, 0x00060111, 0x00000a1b),
    12: (0x00022020, 0x00080d0a, 0x00060111, 0x00000a1b),
    13: (0x00022020, 0x00080d0e, 0x00060111, 0x00000a1b),
    14: (0x00022020, 0x00080d1a, 0x00060111, 0x00000a03),
}

RF_VALS_2525E: dict[int, tuple[int, int, int, int]] = {
    1:  (0x00022010, 0x0000089a, 0x00060111, 0x00000e1b),
    2:  (0x00022010, 0x0000089e, 0x00060111, 0x00000e07),
    3:  (0x00022010, 0x0000089e, 0x00060111, 0x00000e1b),
    4:  (0x00022010, 0x000008a2, 0x00060111, 0x00000e07),
    5:  (0x00022010, 0x000008a2, 0x00060111, 0x00000e1b),
    6:  (0x00022010, 0x000008a6, 0x00060111, 0x00000e07),
    7:  (0x00022010, 0x000008a6, 0x00060111, 0x00000e1b),
    8:  (0x00022010, 0x000008aa, 0x00060111, 0x00000e07),
    9:  (0x00022010, 0x000008aa, 0x00060111, 0x00000e1b),
    10: (0x00022010, 0x000008ae, 0x00060111, 0x00000e07),
    11: (0x00022010, 0x000008ae, 0x00060111, 0x00000e1b),
    12: (0x00022010, 0x000008b2, 0x00060111, 0x00000e07),
    13: (0x00022010, 0x000008b2, 0x00060111, 0x00000e1b),
    14: (0x00022010, 0x000008b6, 0x00060111, 0x00000e23),
}

# RF2525E half-band-higher RF[2] pre-write (rt2500usb.c:594-599), per channel.
RF2525E_HALFBAND: dict[int, int] = {
    1: 0x000008aa, 2: 0x000008ae, 3: 0x000008ae, 4: 0x000008b2,
    5: 0x000008b2, 6: 0x000008b6, 7: 0x000008b6, 8: 0x000008ba,
    9: 0x000008ba, 10: 0x000008be, 11: 0x000008b7, 12: 0x00000902,
    13: 0x00000902, 14: 0x00000906,
}

# rt2500usb.c:1617-1630 (RF5222, 2.4 GHz rows only; 5 GHz rows omitted — the
# driver is 2.4 GHz-only). RF5222 shares RF2525E/RF5222's TX I/Q flip but has
# no half-band pre-tune (that is RF2525E-only in config_channel).
RF_VALS_5222: dict[int, tuple[int, int, int, int]] = {
    1:  (0x00022020, 0x00001136, 0x00000101, 0x00000a0b),
    2:  (0x00022020, 0x0000113a, 0x00000101, 0x00000a0b),
    3:  (0x00022020, 0x0000113e, 0x00000101, 0x00000a0b),
    4:  (0x00022020, 0x00001182, 0x00000101, 0x00000a0b),
    5:  (0x00022020, 0x00001186, 0x00000101, 0x00000a0b),
    6:  (0x00022020, 0x0000118a, 0x00000101, 0x00000a0b),
    7:  (0x00022020, 0x0000118e, 0x00000101, 0x00000a0b),
    8:  (0x00022020, 0x00001192, 0x00000101, 0x00000a0b),
    9:  (0x00022020, 0x00001196, 0x00000101, 0x00000a0b),
    10: (0x00022020, 0x0000119a, 0x00000101, 0x00000a0b),
    11: (0x00022020, 0x0000119e, 0x00000101, 0x00000a0b),
    12: (0x00022020, 0x000011a2, 0x00000101, 0x00000a0b),
    13: (0x00022020, 0x000011a6, 0x00000101, 0x00000a0b),
    14: (0x00022020, 0x000011ae, 0x00000101, 0x00000a1b),
}

_RF_TABLES: dict[int, dict[int, tuple[int, int, int, int]]] = {
    RF2522: RF_VALS_2522, RF2523: RF_VALS_2523, RF2524: RF_VALS_2524,
    RF2525: RF_VALS_2525, RF2525E: RF_VALS_2525E, RF5222: RF_VALS_5222,
}

# The one RF chip this port is hardware-verified against (the RT2500USB.md unit).
VERIFIED_RF = RF2525E
# Fallback for an RF value outside the six the kernel knows: plain rf1..rf4
# writes with no RF2525E half-band pre-tune — a best-effort "give it a shot"
# rather than a hard failure. Warned once per unknown RF type (below).
_FALLBACK_RF = RF2525
_warned_unknown_rf: set[int] = set()


def is_rf_ported(rf_type: int) -> bool:
    """True if a kernel rf_vals table exists for ``rf_type`` (one of the six
    RT2500 RF chips). Used by the driver to log an untested-variant notice."""
    return rf_type in _RF_TABLES


def rf_write(t: RT2500USBTransport, word: int, value: int) -> bool:
    """Serially program RF register ``word`` with a 20-bit ``value``
    (rt2500usb.c:179-206). Returns False if the RF stayed busy.

    PHY_CSR9 takes the low 16 bits; PHY_CSR10 takes bits 16-19 plus the
    20-bit-count + RF_BUSY trigger. ``word`` is not transmitted to the
    chip (RF has no addressing here); it's kept only to mirror the kernel
    call shape and aid logging.
    """
    available, _ = t.regbusy_read(PHY_CSR10, PHY_CSR10_RF_BUSY)
    if not available:
        logger.warning("rf_write(RF[%d]=0x%05x): RF stayed busy", word, value)
        return False

    t.write16(PHY_CSR9, set_field16(0, PHY_CSR9_RF_VALUE, value & 0xFFFF))

    reg = 0
    reg = set_field16(reg, PHY_CSR10_RF_VALUE, (value >> 16) & 0xFF)
    reg = set_field16(reg, PHY_CSR10_RF_NUMBER_OF_BITS, 20)
    reg = set_field16(reg, PHY_CSR10_RF_IF_SELECT, 0)
    reg = set_field16(reg, PHY_CSR10_RF_BUSY, 1)
    t.write16(PHY_CSR10, reg)
    return True


def config_channel(
    t: RT2500USBTransport,
    rf_type: int,
    channel: int,
    txpower: int = DEFAULT_TXPOWER,
) -> bool:
    """Tune the RF to ``channel`` (rt2500usb.c:582-611).

    ``rf_type`` is the per-card EEPROM RF chip (EEPROM_ANTENNA_RF_TYPE); each
    of the six known chips has its own rf_vals table. An RF value outside that
    set (a chip the kernel itself doesn't know) falls back to the RF2525 table
    and logs once — a "give it a shot" instead of a hard failure. Only RF2525E
    gets the half-band pre-tune below, so the fallback stays plain-write.

    Returns True if every RF write completed without an RF_BUSY timeout.
    """
    table = _RF_TABLES.get(rf_type)
    if table is None:
        if rf_type not in _warned_unknown_rf:
            logger.warning("config_channel: unknown RF type 0x%x (not one of the six "
                           "RT2500 chips) — untested; tuning with the RF2525 fallback "
                           "table", rf_type)
            _warned_unknown_rf.add(rf_type)
        table = _RF_TABLES[_FALLBACK_RF]
    if channel not in table:
        raise ValueError(f"channel {channel} out of range for this RF")

    rf1, rf2, rf3, rf4 = table[channel]

    # TXpower into RF[3] (set_field16 is width-agnostic; RF3_TXPOWER is a
    # 32-bit-space mask). TXPOWER_TO_DEV = clamp(txpower, 0, 31).
    txp = max(MIN_TXPOWER, min(MAX_TXPOWER, txpower))
    rf3 = set_field16(rf3, RF3_TXPOWER, txp)

    ok = True
    # RF2525E: pre-tune RF[2] half a band higher, then RF[4] (584-604).
    if rf_type == RF2525E:
        ok &= rf_write(t, 2, RF2525E_HALFBAND[channel])
        if rf4:
            ok &= rf_write(t, 4, rf4)

    ok &= rf_write(t, 1, rf1)
    ok &= rf_write(t, 2, rf2)
    ok &= rf_write(t, 3, rf3)
    if rf4:
        ok &= rf_write(t, 4, rf4)
    return ok


def config_txpower(
    t: RT2500USBTransport,
    txpower: int = DEFAULT_TXPOWER,
    rf3: int = 0,
) -> bool:
    """Set TX power without retuning (rt2500usb_config_txpower, 613-621).

    The first ``rt2x00mac_config`` after radio-on runs CONF_CHANGE_POWER with
    no channel: read RF[3] (the rt2x00 RF cache — 0 before any channel tune),
    splice TXPOWER_TO_DEV into RF3_TXPOWER, rf_write(3). Channel tunes carry the
    TX power in RF[3] themselves, so this standalone call happens just once.
    """
    txp = max(MIN_TXPOWER, min(MAX_TXPOWER, txpower))
    return rf_write(t, 3, set_field16(rf3, RF3_TXPOWER, txp))


def set_channel(
    t: RT2500USBTransport,
    rf_type: int,
    channel: int,
    txpower: int = DEFAULT_TXPOWER,
) -> bool:
    """Public channel-tune entry point. Thin wrapper over config_channel
    so the driver/UI has a stable name."""
    return config_channel(t, rf_type, channel, txpower)


def antenna_defaults(antenna_word: int) -> tuple[int, int]:
    """Extract (tx, rx) default antenna from the EEPROM_ANTENNA word, with
    SW_DIVERSITY → HW_DIVERSITY substitution (rt2500usb.c:1461-1475)."""
    tx = get_field16(antenna_word, EEPROM_ANTENNA_TX_DEFAULT)
    rx = get_field16(antenna_word, EEPROM_ANTENNA_RX_DEFAULT)
    if tx == ANTENNA_SW_DIVERSITY:
        tx = ANTENNA_HW_DIVERSITY
    if rx == ANTENNA_SW_DIVERSITY:
        rx = ANTENNA_HW_DIVERSITY
    return tx, rx


def config_ant(t: RT2500USBTransport, rf_type: int, ant_tx: int, ant_rx: int) -> None:
    """Port of rt2500usb_config_ant (rt2500usb.c:500-580).

    Sets the TX/RX antenna selection (BBP R2/R14 + PHY_CSR5/6) and, for
    RF2525E/RF5222, the TX I/Q flip. RF2525E does *not* flip RX I/Q. This
    matters for correct demodulation on RF2525E, so it runs as part of the
    RX bring-up. ``ant_tx``/``ant_rx`` come from antenna_defaults().
    """
    r2 = bbp_read(t, 2)
    r14 = bbp_read(t, 14)
    csr5 = t.read16(PHY_CSR5)
    csr6 = t.read16(PHY_CSR6)

    # TX antenna.
    if ant_tx == ANTENNA_HW_DIVERSITY:
        r2 = set_field16(r2, BBP_R2_TX_ANTENNA, 1)
        csr5 = set_field16(csr5, PHY_CSR5_CCK, 1)
        csr6 = set_field16(csr6, PHY_CSR6_OFDM, 1)
    elif ant_tx == ANTENNA_A:
        r2 = set_field16(r2, BBP_R2_TX_ANTENNA, 0)
        csr5 = set_field16(csr5, PHY_CSR5_CCK, 0)
        csr6 = set_field16(csr6, PHY_CSR6_OFDM, 0)
    else:   # ANTENNA_B / default
        r2 = set_field16(r2, BBP_R2_TX_ANTENNA, 2)
        csr5 = set_field16(csr5, PHY_CSR5_CCK, 2)
        csr6 = set_field16(csr6, PHY_CSR6_OFDM, 2)

    # RX antenna.
    if ant_rx == ANTENNA_HW_DIVERSITY:
        r14 = set_field16(r14, BBP_R14_RX_ANTENNA, 1)
    elif ant_rx == ANTENNA_A:
        r14 = set_field16(r14, BBP_R14_RX_ANTENNA, 0)
    else:   # ANTENNA_B / default
        r14 = set_field16(r14, BBP_R14_RX_ANTENNA, 2)

    # RF2525E / RF5222 need TX I/Q flip; RF2525E keeps RX I/Q unflipped.
    if rf_type in (RF2525E, RF5222):
        r2 = set_field16(r2, BBP_R2_TX_IQ_FLIP, 1)
        csr5 = set_field16(csr5, PHY_CSR5_CCK_FLIP, 1)
        csr6 = set_field16(csr6, PHY_CSR6_OFDM_FLIP, 1)
        if rf_type == RF2525E:
            r14 = set_field16(r14, BBP_R14_RX_IQ_FLIP, 0)
    else:
        csr5 = set_field16(csr5, PHY_CSR5_CCK_FLIP, 0)
        csr6 = set_field16(csr6, PHY_CSR6_OFDM_FLIP, 0)

    bbp_write(t, 2, r2)
    bbp_write(t, 14, r14)
    t.write16(PHY_CSR5, csr5)
    t.write16(PHY_CSR6, csr6)
