"""RTL8821AU (DKMS) RF/BB register I/O (SIPI) + M3 RadioA table.

phy_RFSerialWrite encodes (addr<<20 | data) to rf3wireOffset (0xC90 path A / 0xE90
path B). A *masked* RF write (BitMask != the full 20-bit mask) first reads the RF
reg via the SIPI read path (phy_RFSerialRead: query PI-mode at 0xC00/0xE00 bit2,
write the read-addr to rHSSIRead 0x8B0, read back from the SI/PI readback reg),
then writes. The 8821 skips the rCCAonSec toggle that the 8812 C-cut does.
[SRC] rtl8812a_phycfg.c:96-220; Hal8812PhyReg.h:57-67,280.

# TODO(8812au): path B is a real radio on 8812 (rf_reg_path_num=2, RADIO_B table).
"""
from __future__ import annotations

from .phy_cond import JaguarParams, apply_table
from .rf_radioa_tbl import RF_RADIOA

RF_PATH_A, RF_PATH_B = 0, 1
RF_CHNLBW = 0x18                   # RF_CHNLBW_Jaguar
RFREG_WRITE_MASK = 0x000FFFFF      # bLSSIWrite_data_Jaguar (full -> direct write, no read)
REG_HSSI_READ = 0x08B0            # rHSSIRead_Jaguar
_RF3WIRE = {RF_PATH_A: 0x0C90, RF_PATH_B: 0x0E90}
_PI_MODE_REG = {RF_PATH_A: 0x0C00, RF_PATH_B: 0x0E00}
_SI_READBACK = {RF_PATH_A: 0x0D08, RF_PATH_B: 0x0D48}
_PI_READBACK = {RF_PATH_A: 0x0D04, RF_PATH_B: 0x0D44}
_RF_DELAY_ADDRS = {0xFE, 0xFFE, 0xFD, 0xFC, 0xFB, 0xFA, 0xF9}


def _shift(mask: int) -> int:
    return (mask & -mask).bit_length() - 1 if mask else 0


def query_bb(t, reg: int, mask: int) -> int:
    return (t.read32(reg) & mask) >> _shift(mask)


def set_bb(t, reg: int, mask: int, val: int) -> None:
    """phy_set_bb_reg: full-dword write when mask is all-ones, else read-modify-write."""
    if mask == 0xFFFFFFFF:
        t.write32(reg, val & 0xFFFFFFFF)
        return
    cur = t.read32(reg)
    t.write32(reg, (cur & ~mask & 0xFFFFFFFF) | ((val << _shift(mask)) & mask))


def _rf_serial_read(t, path: int, offset: int) -> int:
    is_pi = query_bb(t, _PI_MODE_REG[path], 0x4)            # bit2 = PI vs SI mode
    set_bb(t, REG_HSSI_READ, 0xFF, offset & 0xFF)           # latch read addr
    # udelay(20) — no-op under replay
    rb = _PI_READBACK[path] if is_pi else _SI_READBACK[path]
    return query_bb(t, rb, 0x000FFFFF)


def set_rf_reg(t, path: int, addr: int, mask: int, val: int) -> None:
    """PHY_SetRFReg8812: masked write reads-modifies via SIPI; full mask writes direct."""
    if mask != RFREG_WRITE_MASK:
        orig = _rf_serial_read(t, path, addr)
        val = (orig & ~mask) | (val << _shift(mask))
    t.write32(_RF3WIRE[path], (((addr & 0xFF) << 20) | (val & 0xFFFFF)) & 0x0FFFFFFF)


def _radioa_write(t, addr, data):
    if addr in _RF_DELAY_ADDRS:
        return                                              # delay pseudo-addr — no write
    set_rf_reg(t, RF_PATH_A, addr, RFREG_WRITE_MASK, data)


def phy_rf_config(t, params: JaguarParams | None = None) -> None:
    """M3 part 2: RadioA table (path-A SIPI writes)."""
    apply_table(RF_RADIOA, lambda a, d: _radioa_write(t, a, d), params or JaguarParams())
