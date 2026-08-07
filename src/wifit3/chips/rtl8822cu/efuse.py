"""RTL8822CU EFUSE read and logical-map decoder.

The physical 512-byte WIFI EFUSE is read through ``REG_EFUSE_CTRL`` and
expanded into the RTL8822C 768-byte logical shadow map. The control-register
writes select an address only; they never program EFUSE cells.
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    BIT_AUTOLOAD_SUS,
    BIT_EF_READY,
    EEPROM_ID,
    EEPROM_MAC_ADDR,
    EEPROM_RFE_OPTION,
    EEPROM_SIZE,
    EFUSE_ADDR_MASK,
    EFUSE_PROTECTED_SIZE,
    EFUSE_SIZE,
    REG_EFUSE_CTRL,
    REG_SYS_EEPROM_CTRL,
)


@dataclass(frozen=True)
class EfuseInfo:
    autoload_ok: bool
    map_valid: bool
    logical_map: bytes
    physical_map: bytes

    @property
    def mac_address(self) -> bytes:
        return self.logical_map[EEPROM_MAC_ADDR:EEPROM_MAC_ADDR + 6]

    @property
    def rfe_type(self) -> int:
        """RTL8822C RF front-end type programmed by the board vendor."""
        value = self.logical_map[EEPROM_RFE_OPTION]
        if value == 0xFF:
            raise RuntimeError("RTL8822CU EFUSE has no RFE type at 0xCA")
        return value


def read_physical_map(transport) -> bytes:
    """Read the WIFI physical EFUSE map through the indirect controller."""
    current = transport.read32(REG_EFUSE_CTRL)
    out = bytearray(EFUSE_SIZE)
    for addr in range(EFUSE_SIZE):
        request = current & ~(0xFF | (EFUSE_ADDR_MASK << 8) | BIT_EF_READY)
        transport.write32(REG_EFUSE_CTRL, request | ((addr & EFUSE_ADDR_MASK) << 8))
        for _ in range(1000):
            current = transport.read32(REG_EFUSE_CTRL)
            if current & BIT_EF_READY:
                out[addr] = current & 0xFF
                break
        else:
            raise RuntimeError(f"RTL8822CU EFUSE read timed out at 0x{addr:03x}")
    return bytes(out)


def decode_logical_map(physical_map: bytes) -> bytes:
    """Decode Realtek's word-enabled physical EFUSE stream into its shadow map."""
    if len(physical_map) != EFUSE_SIZE:
        raise ValueError(f"RTL8822CU physical EFUSE must be {EFUSE_SIZE} bytes")
    logical = bytearray(b"\xff" * EEPROM_SIZE)
    end = EFUSE_SIZE - EFUSE_PROTECTED_SIZE
    idx = 0
    while idx < end:
        header = physical_map[idx]
        idx += 1
        if header == 0xFF:
            break
        if (header & 0x1F) == 0x0F:
            if idx >= end or physical_map[idx] == 0xFF:
                break
            header2 = physical_map[idx]
            idx += 1
            block = ((header2 & 0xF0) >> 1) | ((header >> 5) & 0x07)
            word_enable = header2 & 0x0F
        else:
            block = header >> 4
            word_enable = header & 0x0F
        for word in range(4):
            if word_enable & (1 << word):
                continue
            target = block * 8 + word * 2
            if idx + 1 >= end or target + 1 >= EEPROM_SIZE:
                raise RuntimeError("RTL8822CU EFUSE map is malformed")
            logical[target:target + 2] = physical_map[idx:idx + 2]
            idx += 2
    return bytes(logical)


def read_efuse(transport) -> EfuseInfo:
    autoload_ok = not bool(transport.read8(REG_SYS_EEPROM_CTRL) & BIT_AUTOLOAD_SUS)
    physical_map = read_physical_map(transport)
    logical_map = decode_logical_map(physical_map)
    map_valid = int.from_bytes(logical_map[:2], "little") == EEPROM_ID
    return EfuseInfo(autoload_ok, map_valid, logical_map, physical_map)
