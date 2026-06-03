"""RTL8814AU EFUSE read + chip-param decode — vendor faithful port.

The probe phase reads the burned-in efuse to recover per-card parameters the BB
config needs: ``rfe_type`` (phy_cond walker discriminator), ``crystal_cap`` (AFE
trim), and the ``mac_address``. The vendor path is ReadAdapterInfo8814AU ->
hal_InitPGData_8814A -> EFUSE_ShadowMapUpdate -> hal_EfuseReadEFuse8814A.

Mechanism (all [SRC] hal_EfuseReadEFuse8814A:1646 + the per-byte EFUSE_CTRL
protocol; [WIRE] cap1 frames 51..5677, device 51, 312 physical bytes):

  * Physical read: each byte is a 9-transfer EFUSE_CTRL cycle (bank-select 0x34,
    address 0x31/0x32, trigger 0x33 bit7=0, poll 0x33 bit7, data 0x30).
  * Header unpacking: the physical efuse is a stream of PG blocks. Each block has
    a header (section offset + 4-bit word-enable); enabled words contribute two
    bytes to ``eFuseWord[section][word]``. The 16x4 words flatten into a 512 B
    logical map (``section*8 + word*2``).
  * Parse: rfe_type/crystal_cap/mac_address read at fixed logical offsets.

The whole read is gated by REG_EFUSE_ACCESS (0x69 on, 0x00 off). cut_version /
package_type come from REG_SYS_CFG1 (read but not decoded — they don't gate this
card's BB walker; see bb.py).
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from . import constants as C


class ChipParams(NamedTuple):
    rfe_type: int
    crystal_cap: int
    mac_address: Optional[str]
    chip_version: int
    autoload_fail: bool


def _efuse_one_byte_read(t, addr: int) -> int:
    """[SRC] efuse_OneByteRead (halmac per-byte EFUSE_CTRL protocol).

    Select the WIFI efuse bank, set the 10-bit physical address, trigger a read
    (clear EFUSE_CTRL+3 bit7), poll until it is set again, then read the byte.
    """
    v = t.read16(C.REG_EFUSE_TEST)
    t.write16(C.REG_EFUSE_TEST, v & ~C.EFUSE_SEL_MASK)          # WIFI bank 0
    t.write8(C.REG_EFUSE_CTRL + 1, addr & 0xFF)                 # addr[7:0]
    v = t.read8(C.REG_EFUSE_CTRL + 2)
    t.write8(C.REG_EFUSE_CTRL + 2, (v & 0xFC) | ((addr >> 8) & 0x03))  # addr[9:8]
    v = t.read8(C.REG_EFUSE_CTRL + 3)
    t.write8(C.REG_EFUSE_CTRL + 3, v & ~C.EFUSE_CTRL_VALID)     # trigger read
    for _ in range(1000):
        if t.read8(C.REG_EFUSE_CTRL + 3) & C.EFUSE_CTRL_VALID:
            break
    return t.read8(C.REG_EFUSE_CTRL)


def _read_logical_map(t) -> bytes:
    """[SRC] hal_EfuseReadEFuse8814A — physical efuse stream -> 512 B logical map."""
    word = [[0xFFFF] * C.EFUSE_MAX_WORD_UNIT for _ in range(C.EFUSE_MAX_SECTION)]
    addr = 0

    header = _efuse_one_byte_read(t, addr)
    addr += 1
    if header == 0xFF:
        return b"\xFF" * C.EFUSE_MAP_LEN          # empty efuse

    while header != 0xFF and addr < C.EFUSE_REAL_CONTENT_LEN:
        if (header & 0x1F) == 0x0F:               # EXT_HEADER
            offset_2_0 = (header & 0xE0) >> 5
            ext = _efuse_one_byte_read(t, addr)
            addr += 1
            if ext == 0xFF:
                break
            if (ext & 0x0F) == 0x0F:              # ALL_WORDS_DISABLED
                header = _efuse_one_byte_read(t, addr)
                addr += 1
                break
            offset = ((ext & 0xF0) >> 1) | offset_2_0
            wden = ext & 0x0F
        else:
            offset = (header >> 4) & 0x0F
            wden = header & 0x0F

        if offset < C.EFUSE_MAX_SECTION:
            for i in range(C.EFUSE_MAX_WORD_UNIT):
                if wden & (1 << i):               # word disabled
                    continue
                data = _efuse_one_byte_read(t, addr)
                addr += 1
                word[offset][i] = data & 0xFF
                if addr >= C.EFUSE_REAL_CONTENT_LEN:
                    break
                data = _efuse_one_byte_read(t, addr)
                addr += 1
                word[offset][i] |= (data << 8) & 0xFF00
                if addr >= C.EFUSE_REAL_CONTENT_LEN:
                    break
        else:                                     # invalid offset — skip its words
            for i in range(C.EFUSE_MAX_WORD_UNIT):
                if wden & 0x01:
                    continue
                addr += 1
                if addr >= C.EFUSE_REAL_CONTENT_LEN:
                    break
                addr += 1
                if addr >= C.EFUSE_REAL_CONTENT_LEN:
                    break

        # Read the next PG header at the current address (advance only if valid).
        header = _efuse_one_byte_read(t, addr)
        if header != 0xFF:
            addr += 1

    tbl = bytearray(b"\xFF" * C.EFUSE_MAP_LEN)
    for i in range(C.EFUSE_MAX_SECTION):
        for j in range(C.EFUSE_MAX_WORD_UNIT):
            tbl[i * 8 + j * 2] = word[i][j] & 0xFF
            tbl[i * 8 + j * 2 + 1] = (word[i][j] >> 8) & 0xFF
    return bytes(tbl)


def _parse_rfe_type(m: bytes) -> int:
    """[SRC] hal_ReadRFEType_8814A — efuse 0xCA[6:0], else 8814AU fallback (1)."""
    v = m[C.EEPROM_RFE_OPTION]
    if v == 0xFF or (v & 0x80):
        return C.RFE_TYPE_8814AU_FALLBACK
    return v & 0x7F


def _parse_crystal_cap(m: bytes) -> int:
    """[SRC] hal_EfuseParseXtal_8814A — efuse 0xB9, else default 0x20."""
    v = m[C.EEPROM_XTAL]
    return C.EEPROM_DEFAULT_CRYSTAL_CAP if v == 0xFF else v


def _parse_mac_address(m: bytes) -> Optional[str]:
    """[SRC] hal_config_macaddr — efuse 0xD8..0xDD; None if blank/invalid."""
    mac = m[C.EEPROM_MAC_ADDR:C.EEPROM_MAC_ADDR + 6]
    if len(mac) != 6 or all(b == 0xFF for b in mac) or all(b == 0 for b in mac):
        return None
    return ":".join(f"{b:02x}" for b in mac)


def read_chip_params(t) -> ChipParams:
    """Probe-phase chip-info + efuse read. [WIRE] cap1 frames 51..5677.

    Reproduces the capture's pre-power-on sequence: chip-version read, autoload
    check, EFUSE access-on, the byte loop, access-off. Returns the decoded params
    the BB config consumes.
    """
    chip_version = t.read32(C.REG_SYS_CFG1)                 # ReadChipVersion
    ee = t.read8(C.REG_9346CR)
    autoload_fail = not (ee & C.EEPROM_EN)

    t.write8(C.REG_EFUSE_ACCESS, C.EFUSE_ACCESS_ON)
    t.read16(0x0002)                                        # efuse power-on status
    t.read16(0x0008)
    m = _read_logical_map(t)
    t.write8(C.REG_EFUSE_ACCESS, C.EFUSE_ACCESS_OFF)

    return ChipParams(
        rfe_type=_parse_rfe_type(m),
        crystal_cap=_parse_crystal_cap(m),
        mac_address=_parse_mac_address(m),
        chip_version=chip_version,
        autoload_fail=autoload_fail,
    )
