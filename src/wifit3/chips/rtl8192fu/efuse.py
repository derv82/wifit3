from __future__ import annotations

import time
from dataclasses import dataclass

from . import constants as C
from .transport import RTL8192FUTransport


@dataclass(frozen=True)
class TxPowerPath:
    cck_base: tuple[int, ...]
    ht40_base: tuple[int, ...]
    ofdm_diff: tuple[int, ...]
    ht20_diff: tuple[int, ...]
    ht40_diff: tuple[int, ...]


@dataclass(frozen=True)
class EfuseData:
    mac_address: bytes
    default_crystal_cap: int
    rfe_type: int
    path_a: TxPowerPath
    path_b: TxPowerPath


def _signed_nibble(value: int) -> int:
    value &= 0x0F
    return value - 16 if value & 0x08 else value


def _read_efuse8(transport: RTL8192FUTransport, offset: int) -> int:
    transport.write8(C.REG_EFUSE_CTRL + 1, offset & 0xFF)
    value = transport.read8(C.REG_EFUSE_CTRL + 2)
    transport.write8(C.REG_EFUSE_CTRL + 2, (value & 0xFC) | ((offset >> 8) & 0x03))
    value = transport.read8(C.REG_EFUSE_CTRL + 3)
    transport.write8(C.REG_EFUSE_CTRL + 3, value & 0x7F)
    transport.read32(C.REG_EFUSE_CTRL)
    for _ in range(C.RTL8XXXU_MAX_REG_POLL):
        if transport.read32(C.REG_EFUSE_CTRL) & (1 << 31):
            break
    else:
        raise IOError(f"EFUSE read timed out at physical offset {offset:#x}")
    time.sleep(0.00005)
    return transport.read32(C.REG_EFUSE_CTRL) & 0xFF


def read_efuse(transport: RTL8192FUTransport) -> bytes:
    transport.read16(C.REG_9346CR)
    value = transport.read32(C.REG_EFUSE_TEST)
    transport.write32(C.REG_EFUSE_TEST, value & ~C.EFUSE_SELECT_MASK)
    transport.write8(C.REG_EFUSE_ACCESS, C.EFUSE_ACCESS_ENABLE)
    try:
        value = transport.read16(C.REG_SYS_ISO_CTRL)
        if not value & C.SYS_ISO_PWC_EV12V:
            transport.write16(C.REG_SYS_ISO_CTRL, value | C.SYS_ISO_PWC_EV12V)
        value = transport.read16(C.REG_SYS_FUNC)
        if not value & C.SYS_FUNC_ELDR:
            transport.write16(C.REG_SYS_FUNC, value | C.SYS_FUNC_ELDR)
        value = transport.read16(C.REG_SYS_CLKR)
        clocks = C.SYS_CLK_LOADER_ENABLE | C.SYS_CLK_ANA8M
        if value & clocks != clocks:
            transport.write16(C.REG_SYS_CLKR, value | clocks)

        logical = bytearray(b"\xff" * C.EFUSE_MAP_LEN)
        physical = 0
        while physical < C.EFUSE_MAP_LEN:
            header = _read_efuse8(transport, physical)
            physical += 1
            if header == 0xFF:
                break
            if header & 0x1F == 0x0F:
                section = (header & 0xE0) >> 5
                extheader = _read_efuse8(transport, physical)
                physical += 1
                if extheader & 0x0F == 0x0F:
                    continue
                section |= (extheader & 0xF0) >> 1
                word_mask = extheader & 0x0F
            else:
                section = header >> 4
                word_mask = header & 0x0F

            logical_offset = section * 8
            for word in range(4):
                if word_mask & (1 << word):
                    logical_offset += 2
                    continue
                low = _read_efuse8(transport, physical)
                physical += 1
                if logical_offset >= C.EFUSE_MAP_LEN - 1:
                    raise ValueError("EFUSE logical address exceeds map")
                logical[logical_offset] = low
                logical[logical_offset + 1] = _read_efuse8(transport, physical)
                physical += 1
                logical_offset += 2
        return bytes(logical)
    finally:
        transport.write8(C.REG_EFUSE_ACCESS, 0)


def _parse_power_path(data: bytes, offset: int) -> TxPowerPath:
    diff = data[offset + 11]
    ofdm = [0]
    ht20 = [_signed_nibble(diff >> 4)]
    ht40 = [0]
    for index in range(3):
        first = data[offset + 12 + index * 2]
        second = data[offset + 13 + index * 2]
        ht20.append(_signed_nibble(first))
        ht40.append(_signed_nibble(first >> 4))
        ofdm.append(_signed_nibble(second))
    return TxPowerPath(
        cck_base=tuple(data[offset:offset + 6]),
        ht40_base=tuple(data[offset + 6:offset + 11]),
        ofdm_diff=tuple(ofdm),
        ht20_diff=tuple(ht20),
        ht40_diff=tuple(ht40),
    )


def parse_efuse(data: bytes) -> EfuseData:
    if len(data) != C.EFUSE_MAP_LEN:
        raise ValueError(f"invalid EFUSE map size: {len(data)}")
    rtl_id = int.from_bytes(data[:2], "little")
    if rtl_id != 0x8129:
        raise ValueError(f"invalid RTL8192FU EFUSE ID: {rtl_id:#06x}")
    return EfuseData(
        mac_address=data[0x107:0x10D],
        default_crystal_cap=data[0xB9] & 0x3F,
        rfe_type=data[0xCA] & 0x1F,
        path_a=_parse_power_path(data, 0x10),
        path_b=_parse_power_path(data, 0x3A),
    )
