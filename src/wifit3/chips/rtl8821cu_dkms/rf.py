"""RTL8821CU RF radio-A init — ``init_rf_reg`` (the RF half of ``rtl8821c_phy_init``).

[SRC] hal/rtl8821c/rtl8821c_phy.c:207 ``_init_phy_parameter_rf`` -> the RF radio-A PHYDM table
(``odm_config_rf_radioa_8821c`` -> ``odm_config_rf_reg_8821c`` -> ``config_phydm_write_rf_reg
_8821c``). RF registers are written through the path-A LSSI 3-wire port (0x0C90): the packed
value is ``(addr[7:0] << 20) | data[19:0]``. Table addresses 0xFE / 0xFFE are delay opcodes
([SRC] phydm_regconfig8821c.c odm_config_rf_reg_8821c), not register writes. The table carries
cut/rfe/package conditional rows, so it runs through ``phy_cond.walk``. The Tx-power-track config
that follows ([SRC] rtl8821c_phy.c:263) only fills software tables — no register I/O.
"""
from __future__ import annotations

from . import phy_cond
from .rf_radioa_tbl import RADIOA_TBL

REG_LSSI_WRITE_A = 0x0C90          # rA_LSSIWrite_Jaguar — path-A RF 3-wire write port
_SI_READ_A_BASE = 0x2800           # config_phydm_read_rf_reg_8821c: BB read at base + (addr<<2)
_RFREGOFFSETMASK = 0xFFFFF
_RF_DELAY = (0xFE, 0xFFE)          # delay opcodes (us / ms); no register write


def write_rf(t, addr: int, data: int) -> None:
    """config_phydm_write_rf_reg_8821c [SRC] phydm_hal_api8821c.c (full-mask path): pack the RF
    address into bits [27:20] and data into [19:0], write the LSSI port (no read for full mask)."""
    t.write32(REG_LSSI_WRITE_A, (((addr & 0xFF) << 20) | (data & _RFREGOFFSETMASK)) & 0x0FFFFFFF)


def read_rf(t, addr: int) -> int:
    """config_phydm_read_rf_reg_8821c [SRC] phydm_hal_api8821c.c (path A): the RF readback is a
    direct BB read at 0x2800 + (addr<<2) — e.g. RF 0xef reads back at 0x2bbc."""
    return t.read32(_SI_READ_A_BASE + ((addr & 0xFF) << 2)) & _RFREGOFFSETMASK


def write_rf_masked(t, addr: int, mask: int, data: int) -> None:
    """config_phydm_write_rf_reg_8821c masked path — a partial mask read-modify-writes the RF reg
    (read the full reg via ``read_rf``, merge the field at the mask's lowest bit, LSSI-write).
    A full-mask (RFREGOFFSETMASK) write skips the read, same as ``write_rf``."""
    mask &= _RFREGOFFSETMASK
    if mask != _RFREGOFFSETMASK:
        shift = (mask & -mask).bit_length() - 1
        data = (read_rf(t, addr) & ~mask) | (data << shift)
    write_rf(t, addr, data)


def config_radioa(t, cfg: phy_cond.PhyCondConfig) -> None:
    """init_rf_reg [SRC] rtl8821c_phy.c:207 — apply the RF radio-A table via the conditional
    walker; addresses 0xFE/0xFFE are delays (no I/O during replay)."""
    def apply(addr, data):
        if addr not in _RF_DELAY:
            write_rf(t, addr, data)
    phy_cond.walk(RADIOA_TBL, cfg, apply)
