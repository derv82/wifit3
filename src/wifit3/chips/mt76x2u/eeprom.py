"""MT76x2U EEPROM (eFuse) reader.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

The EEPROM (or eFuse-backed virtual EEPROM) is read 4 bytes at a time via
vendor control transfer with bRequest = MT_VEND_READ_EEPROM (0x09). The
virtual-address-bit `MT_VEND_TYPE_EEPROM = BIT(31)` routes the read through
that path in `transport.read32`.

Layout (struct fields from mt76x02_eeprom.h):

    0x000  CHIP_ID         u16
    0x002  VERSION         u16
    0x004  MAC_ADDR        6 bytes
    0x022  ANTENNA         u16
    0x034  NIC_CONF_0      u16  (RX path / TX path / PA type / board)
    0x036  NIC_CONF_1      u16  (LNA_EXT_2G/5G / TX_ALC_EN / HW_RF_CTRL)
    0x03a  FREQ_OFFSET     u8
    ...
"""
from __future__ import annotations

import logging
import struct

from .constants import MT_VEND_TYPE_EEPROM
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)

# Field offsets used at the wifit3 layer — [SRC] mt76x02_eeprom.h.
EE_CHIP_ID      = 0x000
EE_VERSION      = 0x002
EE_MAC_ADDR     = 0x004
EE_NIC_CONF_0   = 0x034
EE_NIC_CONF_1   = 0x036
EE_FREQ_OFFSET  = 0x03A

# Standard mt76x2 EEPROM is 512 bytes; we don't need to slurp the whole thing
# for M2, only the header + key fields.


def read_block(transport: MT76x2UTransport, offset: int, length: int) -> bytes:
    """Read `length` bytes starting at EEPROM `offset`.

    Both args should be multiples of 4 for clean reads. Aligned reads use
    the cached 4-byte path in `transport.read32`; unaligned would need an
    extra read-modify-shift.
    """
    if offset % 4 != 0:
        raise ValueError(f"EEPROM read offset must be 4-aligned (got 0x{offset:x})")
    if length % 4 != 0:
        # Round up so we always read whole words; trim at the end.
        rounded = (length + 3) & ~3
    else:
        rounded = length
    out = bytearray(rounded)
    for i in range(0, rounded, 4):
        word = transport.read32(MT_VEND_TYPE_EEPROM | (offset + i))
        struct.pack_into("<I", out, i, word)
    return bytes(out[:length])


def read_u16(transport: MT76x2UTransport, offset: int) -> int:
    """Read a u16 field from EEPROM."""
    # Round down to 4-byte boundary, then index into the word.
    aligned = offset & ~0x3
    word = transport.read32(MT_VEND_TYPE_EEPROM | aligned)
    shift = (offset - aligned) * 8
    return (word >> shift) & 0xFFFF


def read_mac_address(transport: MT76x2UTransport) -> str:
    """Return the MAC address as `AA:BB:CC:DD:EE:FF`.

    Layout: 6 bytes starting at EE_MAC_ADDR=0x004. Two u32 reads cover it
    (the second read gives 4 bytes but we only use the first 2).
    """
    blob = read_block(transport, EE_MAC_ADDR, 8)
    mac = blob[:6]
    return ":".join(f"{b:02X}" for b in mac)


def read_chip_id(transport: MT76x2UTransport) -> int:
    return read_u16(transport, EE_CHIP_ID)


def read_nic_conf_0(transport: MT76x2UTransport) -> dict:
    """Decode MT_EE_NIC_CONF_0 into human-readable fields.

    [SRC] mt76x02_eeprom.h:100-106.
    """
    val = read_u16(transport, EE_NIC_CONF_0)
    return {
        "raw": val,
        "rx_path": val & 0xF,
        "tx_path": (val >> 4) & 0xF,
        "pa_int_2g": bool(val & (1 << 8)),
        "pa_int_5g": bool(val & (1 << 9)),
        "pa_io_current": bool(val & (1 << 10)),
        "board_type": (val >> 12) & 0x3,
    }


def read_nic_conf_1(transport: MT76x2UTransport) -> dict:
    val = read_u16(transport, EE_NIC_CONF_1)
    return {
        "raw": val,
        "hw_rf_ctrl": bool(val & (1 << 0)),
        "temp_tx_alc": bool(val & (1 << 1)),
        "lna_ext_2g": bool(val & (1 << 2)),
        "lna_ext_5g": bool(val & (1 << 3)),
        "tx_alc_en": bool(val & (1 << 13)),
    }
