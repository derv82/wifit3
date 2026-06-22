"""RTL8821CU EFUSE read — the indirect read loop over REG_EFUSE_CTRL (0x30).

The vendor reads the full 512-byte physical EFUSE through an indirect controller: write the
byte address into bits [17:8] with the ready flag (bit 31) cleared, poll until the chip sets
the flag, then take the data from the low byte. This is the bulk of the cold-boot prologue
(one 4-byte write + poll per byte).

Ported from:
  [SRC] hal/rtl8821c/rtl8821c_ops.c:462          rtl8821c_read_efuse (autoload check + dump)
  [SRC] hal/halmac/halmac_88xx/halmac_efuse_88xx.c:1088  read_hw_efuse_88xx (the 0x30 loop)
  [SRC] hal/halmac/halmac_88xx/halmac_efuse_88xx.c (switch_efuse_bank_88xx)
  [SRC] hal/halmac/halmac_88xx/halmac_8821c/halmac_common_8821c.c:169  cfg_ldo25_8821c
  [SRC] hal/halmac/halmac_88xx/halmac_8821c/halmac_8821c_cfg.h:47      EFUSE_SIZE_8821C = 512
Bit fields [SRC] hal/halmac/halmac_bit_8821c.h: EF_FLAG :689, EF_ADDR shift/mask :727-731,
EF_DATA mask :739, AUTOLOAD_SUS :129.
"""
from __future__ import annotations

REG_SYS_EEPROM_CTRL = 0x000A
REG_EFUSE_CTRL = 0x0030
REG_LDO_EFUSE_CTRL = 0x0034

EFUSE_SIZE_8821C = 512

_BIT_AUTOLOAD_SUS = 1 << 5
_BIT_EF_FLAG = 1 << 31
_SHIFT_EF_ADDR = 8
_MASK_EF_ADDR = 0x3FF
_BITS_EF_ADDR = _MASK_EF_ADDR << _SHIFT_EF_ADDR
_MASK_EF_DATA = 0xFF
_EFUSE_POLL_CNT = 1000000          # [SRC] read_hw_efuse_88xx cnt; replay matches on read #1


def _cfg_ldo25(t, enable: bool) -> None:
    """Toggle the 2.5 V EFUSE LDO via REG_LDO_EFUSE_CTRL+3 bit7.
    [SRC] halmac_common_8821c.c:169 cfg_ldo25_8821c."""
    v = t.read8(REG_LDO_EFUSE_CTRL + 3)
    t.write8(REG_LDO_EFUSE_CTRL + 3, (v | 0x80) if enable else (v & ~0x80))


def _switch_efuse_bank_wifi(t) -> None:
    """Select the WIFI EFUSE bank (0) via REG_LDO_EFUSE_CTRL+1 bits[1:0].
    [SRC] halmac_efuse_88xx.c switch_efuse_bank_88xx — returns early when already WIFI."""
    rv = t.read8(REG_LDO_EFUSE_CTRL + 1)
    if (rv & 0x3) == 0:
        return
    t.write8(REG_LDO_EFUSE_CTRL + 1, rv & ~0x3)
    if (t.read8(REG_LDO_EFUSE_CTRL + 1) & 0x3) != 0:
        raise RuntimeError("RTL8821CU: switch efuse bank to WIFI failed")


def read_hw_efuse(t, offset: int = 0, size: int = EFUSE_SIZE_8821C) -> bytes:
    """[SRC] read_hw_efuse_88xx — read `size` physical EFUSE bytes via the 0x30 controller."""
    _cfg_ldo25(t, False)            # reading EFUSE needs no 2.5 V LDO
    value32 = t.read32(REG_EFUSE_CTRL)
    out = bytearray(size)
    for addr in range(offset, offset + size):
        value32 &= ~(_MASK_EF_DATA | _BITS_EF_ADDR)
        value32 |= (addr & _MASK_EF_ADDR) << _SHIFT_EF_ADDR
        t.write32(REG_EFUSE_CTRL, value32 & ~_BIT_EF_FLAG)
        for _ in range(_EFUSE_POLL_CNT):
            tmp32 = t.read32(REG_EFUSE_CTRL)
            if tmp32 & _BIT_EF_FLAG:
                break
        else:
            raise RuntimeError(f"RTL8821CU: efuse read addr 0x{addr:03x} timed out")
        out[addr - offset] = tmp32 & _MASK_EF_DATA
    return bytes(out)


def read_efuse(t) -> tuple[bool, bytes]:
    """[SRC] rtl8821c_read_efuse steps 1-2: autoload-status check, then the WIFI dump.
    Returns (autoload_ok, 512-byte efuse map)."""
    val8 = t.read8(REG_SYS_EEPROM_CTRL)
    autoload_ok = not (val8 & _BIT_AUTOLOAD_SUS)
    _switch_efuse_bank_wifi(t)
    efuse_map = read_hw_efuse(t)
    return autoload_ok, efuse_map
