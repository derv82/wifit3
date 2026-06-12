"""rt2500usb BBP (baseband) indirect register access + init_bbp.

The BBP is not directly addressable; it is reached through the PHY_CSR7
(data/reg-id/read-control) and PHY_CSR8 (busy) registers. Faithful port
of rt2500usb.c:
  * rt2500usb_bbp_write    (122-143)
  * rt2500usb_bbp_read     (145-177)
  * rt2500usb_wait_bbp_ready (882-895)
  * rt2500usb_init_bbp     (898-951)

Verified against usb_dumps/captures_rt2500usb/capture-2: the 31 fixed
BBP writes land at frames 311..(503) and match the kernel list 1:1;
frame 303 is the bbp_read(0) issued by wait_bbp_ready. The EEPROM
override loop (words 0x0e-0x1d) is applied last.
"""
from __future__ import annotations

import logging
import time

from .constants import (
    EEPROM_BBP_REG_ID,
    EEPROM_BBP_SIZE,
    EEPROM_BBP_START,
    EEPROM_BBP_VALUE,
    EEPROM_BBPTUNE_R17,
    EEPROM_BBPTUNE_R24,
    EEPROM_BBPTUNE_R24_LOW,
    EEPROM_BBPTUNE_R24_LOW_DEFAULT,
    EEPROM_BBPTUNE_R25,
    EEPROM_BBPTUNE_R25_LOW,
    EEPROM_BBPTUNE_R25_LOW_DEFAULT,
    EEPROM_BBPTUNE_R61,
    EEPROM_BBPTUNE_R61_LOW,
    EEPROM_BBPTUNE_R61_LOW_DEFAULT,
    EEPROM_BBPTUNE_VGC,
    EEPROM_BBPTUNE_VGCUPPER,
    EEPROM_BBPTUNE_VGCUPPER_DEFAULT,
    PHY_CSR7,
    PHY_CSR7_DATA,
    PHY_CSR7_READ_CONTROL,
    PHY_CSR7_REG_ID,
    PHY_CSR8,
    PHY_CSR8_BUSY,
    REGISTER_BUSY_DELAY,
    REGISTER_USB_BUSY_COUNT,
)
from .transport import RT2500USBTransport, get_field16, set_field16

logger = logging.getLogger(__name__)

# Fixed BBP register defaults (rt2500usb.c:908-938), in write order.
BBP_INIT_VALUES: list[tuple[int, int]] = [
    (3, 0x02), (4, 0x19), (14, 0x1c), (15, 0x30), (16, 0xac),
    (18, 0x18), (19, 0xff), (20, 0x1e), (21, 0x08), (22, 0x08),
    (23, 0x08), (24, 0x80), (25, 0x50), (26, 0x08), (27, 0x23),
    (30, 0x10), (31, 0x2b), (32, 0xb9), (34, 0x12), (35, 0x50),
    (39, 0xc4), (40, 0x02), (41, 0x60), (53, 0x10), (54, 0x18),
    (56, 0x08), (57, 0x10), (58, 0x08), (61, 0x60), (62, 0x10),
    (75, 0xff),
]


def bbp_write(t: RT2500USBTransport, word: int, value: int) -> None:
    """Write ``value`` to BBP register ``word`` (rt2500usb.c:122-143)."""
    available, _ = t.regbusy_read(PHY_CSR8, PHY_CSR8_BUSY)
    if not available:
        logger.warning("bbp_write(%d): BBP busy, dropping write", word)
        return
    reg = 0
    reg = set_field16(reg, PHY_CSR7_DATA, value)
    reg = set_field16(reg, PHY_CSR7_REG_ID, word)
    reg = set_field16(reg, PHY_CSR7_READ_CONTROL, 0)
    t.write16(PHY_CSR7, reg)


def bbp_read(t: RT2500USBTransport, word: int) -> int:
    """Read BBP register ``word`` (rt2500usb.c:145-177).

    Returns 0xff if the BBP never becomes available (matches the kernel,
    where a failed busy-poll leaves reg=0xffff → DATA field = 0xff).
    """
    available, reg = t.regbusy_read(PHY_CSR8, PHY_CSR8_BUSY)
    if available:
        reg = 0
        reg = set_field16(reg, PHY_CSR7_REG_ID, word)
        reg = set_field16(reg, PHY_CSR7_READ_CONTROL, 1)
        t.write16(PHY_CSR7, reg)

        available, reg = t.regbusy_read(PHY_CSR8, PHY_CSR8_BUSY)
        if available:
            reg = t.read16(PHY_CSR7)
    return get_field16(reg, PHY_CSR7_DATA)


def wait_bbp_ready(t: RT2500USBTransport) -> bool:
    """Poll BBP[0] until it reads a sane value (rt2500usb.c:882-895).

    BBP[0] reading 0x00 or 0xff means the baseband isn't up yet.
    """
    for _ in range(REGISTER_USB_BUSY_COUNT):
        value = bbp_read(t, 0)
        if value != 0xFF and value != 0x00:
            return True
        time.sleep(REGISTER_BUSY_DELAY / 1_000_000)
    return False


def _eeprom_word(eeprom: bytes, word: int) -> int:
    """Read a 16-bit LE word from a one-shot EEPROM byte buffer."""
    off = word * 2
    return eeprom[off] | (eeprom[off + 1] << 8)


def eeprom_bbp_overrides(eeprom: bytes) -> list[tuple[int, int]]:
    """Decode the EEPROM BBP-override table (words 0x0e-0x1d).

    Each non-blank word packs REG_ID in the high byte and VALUE in the
    low byte (rt2500usb.c:940-948). 0x0000 / 0xffff words are skipped.
    """
    overrides: list[tuple[int, int]] = []
    for i in range(EEPROM_BBP_SIZE):
        word = _eeprom_word(eeprom, EEPROM_BBP_START + i)
        if word not in (0x0000, 0xFFFF):
            reg_id = get_field16(word, EEPROM_BBP_REG_ID)
            value = get_field16(word, EEPROM_BBP_VALUE)
            overrides.append((reg_id, value))
    return overrides


def _bbptune_byte(eeprom: bytes, word: int, field_mask: int, default: int) -> int:
    """A BBP-tune byte from the EEPROM, with the kernel's blank-word fallback.

    A 0xffff word is uninitialised — ``rt2500usb_init_eeprom`` substitutes the
    per-field default; otherwise the calibrated low byte (or VGCUPPER) is used.
    """
    raw = _eeprom_word(eeprom, word)
    if raw == 0xFFFF:
        return default
    return get_field16(raw, field_mask)


def reset_tuner(t: RT2500USBTransport, eeprom: bytes) -> None:
    """Seed the AGC/VGC baseband registers (rt2500usb.c:689-712).

    rt2x00 calls this on every ``CONF_CHANGE_CHANNEL`` (rt2x00lib_config) and
    on antenna config — unconditionally, monitor mode included. It is the
    *only* gain-control mechanism on this part (rt2500usb has no periodic
    ``link_tuner`` callback), so without it BBP R17 (the variable gain) sits at
    its init value and never tracks the band — the cause of weak / one-AP-only
    RX. R24/R25/R61/R17 come from the per-card EEPROM BBP-tune words.
    """
    bbp_write(t, 24, _bbptune_byte(eeprom, EEPROM_BBPTUNE_R24,
                                   EEPROM_BBPTUNE_R24_LOW, EEPROM_BBPTUNE_R24_LOW_DEFAULT))
    bbp_write(t, 25, _bbptune_byte(eeprom, EEPROM_BBPTUNE_R25,
                                   EEPROM_BBPTUNE_R25_LOW, EEPROM_BBPTUNE_R25_LOW_DEFAULT))
    bbp_write(t, 61, _bbptune_byte(eeprom, EEPROM_BBPTUNE_R61,
                                   EEPROM_BBPTUNE_R61_LOW, EEPROM_BBPTUNE_R61_LOW_DEFAULT))
    bbp_write(t, 17, _bbptune_byte(eeprom, EEPROM_BBPTUNE_VGC,
                                   EEPROM_BBPTUNE_VGCUPPER, EEPROM_BBPTUNE_VGCUPPER_DEFAULT))


def init_bbp(t: RT2500USBTransport, eeprom: bytes) -> None:
    """Port of rt2500usb_init_bbp (rt2500usb.c:898-951).

    Waits for the BBP, applies the 31 fixed defaults, then the
    per-device EEPROM overrides. ``eeprom`` is the one-shot EEPROM byte
    buffer (transport.read_eeprom()).
    """
    if not wait_bbp_ready(t):
        raise IOError("BBP register access failed (BBP[0] never became sane)")

    for reg_id, value in BBP_INIT_VALUES:
        bbp_write(t, reg_id, value)

    for reg_id, value in eeprom_bbp_overrides(eeprom):
        bbp_write(t, reg_id, value)
