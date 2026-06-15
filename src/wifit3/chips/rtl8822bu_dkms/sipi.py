"""RTL8822BU PHYDM BB/RF register primitives (masked RMW + SIPI RF access).

The init register tables (bb.py / rf.py) write whole 32-bit words, but the channel tune needs
masked read-modify-write on BB regs and per-bitfield RF access. These mirror the PHYDM accessors
`[SRC] phydm_hal_api8822b.c`:
  - odm_get_bb_reg / odm_set_bb_reg: BB read / masked-RMW, value shifted into the mask.
  - config_phydm_read_rf_reg_8822b: an RF read is a *direct BB read* at
    `{0x2800,0x2c00}[path] + (addr<<2)` (RFREGOFFSETMASK = 20 bits).
  - config_phydm_write_rf_reg_8822b: an RF write packs `((addr&0xFF)<<20 | data[19:0]) & 0x0FFFFFFF`
    into a W32 at `{0xC90,0xE90}[path]`; a partial bitmask reads the current RF value back first.
"""
from __future__ import annotations

RF_PATH_A, RF_PATH_B = 0, 1
RFREGOFFSETMASK = 0x000FFFFF          # RF registers are 20-bit
_RF_READ_BASE = {RF_PATH_A: 0x2800, RF_PATH_B: 0x2C00}
_RF_WRITE_REG = {RF_PATH_A: 0x0C90, RF_PATH_B: 0x0E90}


def _shift(mask: int) -> int:
    return (mask & -mask).bit_length() - 1   # trailing-zero count = field LSB position


def get_bb_reg(t, addr: int, mask: int) -> int:
    return (t.read32(addr) & mask) >> _shift(mask)


def set_bb_reg(t, addr: int, mask: int, val: int) -> None:
    if mask == 0xFFFFFFFF:
        t.write32(addr, val & 0xFFFFFFFF)
        return
    cur = t.read32(addr)
    t.write32(addr, (cur & ~mask) | ((val << _shift(mask)) & mask))


def read_rf_reg(t, path: int, addr: int, mask: int = RFREGOFFSETMASK) -> int:
    """config_phydm_read_rf_reg_8822b: RF read == direct BB read at the per-path window."""
    direct = _RF_READ_BASE[path] + ((addr & 0xFF) << 2)
    return get_bb_reg(t, direct, mask & RFREGOFFSETMASK)


def set_rf_reg(t, path: int, addr: int, mask: int, val: int) -> None:
    """config_phydm_write_rf_reg_8822b: masked RF write (reads back first when mask is partial)."""
    addr &= 0xFF
    mask &= RFREGOFFSETMASK
    if mask != RFREGOFFSETMASK:
        cur = read_rf_reg(t, path, addr, RFREGOFFSETMASK)
        sh = _shift(mask)
        val = (cur & ~mask) | ((val << sh) & mask)
    data_and_addr = ((addr << 20) | (val & RFREGOFFSETMASK)) & 0x0FFFFFFF
    t.write32(_RF_WRITE_REG[path], data_and_addr)
