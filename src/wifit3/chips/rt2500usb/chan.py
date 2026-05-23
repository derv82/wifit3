"""rt2500usb RF register access + channel tuning.

The RF synthesizer is programmed serially through PHY_CSR9 (low 16 bits)
and PHY_CSR10 (high bits + RF_BUSY trigger + bit count). RF registers are
**write-only** — there is no read path. Faithful port of rt2500usb.c:
  * rt2500usb_rf_write       (179-206)
  * rt2500usb_config_channel (582-611)
  * rf_vals_bg_2525 / 2525e tables (1574-1610)

Verified against usb_dumps/captures_rt2500usb/capture-2 channel-1 set
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
    RF2525,
    RF2525E,
    RF3_TXPOWER,
    RF5222,
)
from .transport import RT2500USBTransport, get_field16, set_field16

logger = logging.getLogger(__name__)

# rf_channel tables: channel -> (rf1, rf2, rf3, rf4). rf3 carries the
# TXPOWER field, overwritten per-tune. rf4 == 0 means "no RF[4] write".
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

_RF_TABLES = {RF2525: RF_VALS_2525, RF2525E: RF_VALS_2525E}


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

    Returns True if every RF write completed without an RF_BUSY timeout.
    """
    table = _RF_TABLES.get(rf_type)
    if table is None:
        raise NotImplementedError(
            f"config_channel: RF type 0x{rf_type:x} not yet ported "
            "(only RF2525 / RF2525E)."
        )
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
