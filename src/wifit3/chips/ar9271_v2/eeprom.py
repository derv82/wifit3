"""EEPROM (4k map) read + validation for the AR9271 (USB).

Ported from eeprom.c / eeprom_4k.c / htc_drv_init.c. Two register paths, both over WMI:
the bulk fill uses batched REG_READ_MULTI directly on the EEPROM word window; single reads
(the magic word) go through the AR_EEPROM_STATUS_DATA access protocol (trigger -> poll ->
value) that ath_usb_eeprom_read implements [SRC] htc_drv_init.c:519.
"""
from __future__ import annotations

import logging
import struct

from . import reg as R
from .hw import AthHw

logger = logging.getLogger(__name__)


def _gen_fill(hw: AthHw, start_loc: int, size: int) -> list[int]:
    """ath9k_hw_usb_gen_fill_eeprom [SRC] eeprom.c:79 — read ``size`` words in 8-word
    REG_READ_MULTI batches from the EEPROM window."""
    words: list[int] = []
    addrs: list[int] = []
    for addr in range(size):
        addrs.append(R.AR5416_EEPROM_OFFSET + ((addr + start_loc) << R.AR5416_EEPROM_S))
        if len(addrs) == 8:
            words.extend(hw.multi_read(addrs))
            addrs = []
    if addrs:
        words.extend(hw.multi_read(addrs))
    return words


def usb_eeprom_read(hw: AthHw, off: int) -> int:
    """ath_usb_eeprom_read [SRC] htc_drv_init.c:519 — trigger a read, wait for the access bit
    to clear, then read the value out of AR_EEPROM_STATUS_DATA."""
    hw.read(R.AR5416_EEPROM_OFFSET + (off << R.AR5416_EEPROM_S))
    if not hw.wait(R.AR_EEPROM_STATUS_DATA,
                   R.AR_EEPROM_STATUS_DATA_BUSY | R.AR_EEPROM_STATUS_DATA_PROT_ACCESS, 0):
        raise RuntimeError("ar9271_v2: EEPROM read timeout")
    return hw.read(R.AR_EEPROM_STATUS_DATA) & R.AR_EEPROM_STATUS_DATA_VAL


def _word(hw: AthHw, idx: int) -> int:
    """LE u16 at word ``idx`` of the filled map4k buffer."""
    return struct.unpack_from("<H", hw.eeprom, idx * 2)[0]


def fill(hw: AthHw) -> None:
    """__ath9k_hw_usb_4k_fill_eeprom [SRC] eeprom_4k.c:52 — read the whole 4k map (188 words
    from word 64) and stash it as raw LE bytes for later field access."""
    words = _gen_fill(hw, R.AR5416_EEP4K_START_LOC, R.SIZE_EEPROM_4K)
    hw.eeprom = bytearray(struct.pack("<%dH" % len(words), *words))


def check(hw: AthHw) -> None:
    """ath9k_hw_4k_check_eeprom [SRC] eeprom_4k.c — magic read + endianness, then checksum
    and version validation over the stashed words (no further wire ops)."""
    magic = usb_eeprom_read(hw, R.AR5416_EEPROM_MAGIC_OFFSET)
    # swab16(magic) == MAGIC means the EEPROM is byte-swapped vs. the host; our card is LE.
    if ((magic >> 8) | ((magic & 0xff) << 8)) == R.AR5416_EEPROM_MAGIC:
        for i in range(len(hw.eeprom) // 2):
            v = _word(hw, i)
            struct.pack_into("<H", hw.eeprom, i * 2, ((v >> 8) | ((v & 0xff) << 8)) & 0xffff)
    elif magic != R.AR5416_EEPROM_MAGIC:
        raise RuntimeError(f"ar9271_v2: invalid EEPROM magic 0x{magic:04x}")

    length = _word(hw, 0)                                   # baseEepHeader.length (word 0)
    el = min(length // 2, R.SIZE_EEPROM_4K)
    checksum = 0
    for i in range(el):
        checksum ^= _word(hw, i)
    if checksum != 0xFFFF:
        raise RuntimeError(f"ar9271_v2: bad EEPROM checksum 0x{checksum:04x}")

    version = _word(hw, 2)                                  # baseEepHeader.version (word 2)
    ver = (version & R.AR5416_EEP_VER_MAJOR_MASK) >> R.AR5416_EEP_VER_MAJOR_SHIFT
    rev = version & R.AR5416_EEP_VER_MINOR_MASK
    if ver != R.AR5416_EEP_VER or rev < R.AR5416_EEP_NO_BACK_VER:
        raise RuntimeError(f"ar9271_v2: bad EEPROM ver 0x{ver:x} rev 0x{rev:x}")
    logger.debug("ar9271_v2: EEPROM ver=0x%x rev=0x%x", ver, rev)


def txgain_type(hw: AthHw) -> int:
    """baseEepHeader.txGainType (4k map byte 31) — get_eeprom(EEP_TXGAIN_TYPE)
    [SRC] eeprom_4k.c:274. 0 = original/normal power, 1 = high power."""
    return hw.eeprom[31]


def init(hw: AthHw) -> None:
    """ath9k_hw_eeprom_init [SRC] eeprom.c:659 — fill then check the 4k map, then latch the
    chain masks (what ath9k_hw_fill_cap_info derives from EEP_RX_MASK/EEP_TX_MASK)."""
    fill(hw)
    check(hw)
    hw.rxchainmask = hw.eeprom[18]                # baseEepHeader.rxMask
    hw.txchainmask = hw.eeprom[19]                # baseEepHeader.txMask
    hw.macaddr = bytearray(hw.eeprom[12:18])      # baseEepHeader.macAddr (init_macaddr)
