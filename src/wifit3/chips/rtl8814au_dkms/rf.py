"""RTL8814AU RF (radio) configuration (M2c) — vendor faithful port.

`PHY_RFConfig8814A` [SRC rtl8814a_phycfg.c:570] -> `PHY_RF6052_Config_8814A` loads
one conditional radio table per RF path A..D through the same phy_cond walker as
the BB tables, then copies the path-A RCK1 calibration to paths B/C/D.

RF register writes do not go to a register at the RF address — each one rides the
per-path LSSI write register (0xc90/0xe90/0x1890/0x1A90) as
``(rf_addr << 20) | (data & 0xFFFFF)`` [SRC] phy_set_rf_reg jaguar. The pseudo
addresses 0xfe/0xffe are 50 ms settling delays, not writes
[SRC odm_config_rf_reg_8814a]. Verified byte-for-byte; [WIRE] cap1 frames 11335+.
"""
from __future__ import annotations

import time

from . import constants as C
from .phy_cond import build_driver1, walk_table
from .rf_radio_a_tbl import RADIO_A
from .rf_radio_b_tbl import RADIO_B
from .rf_radio_c_tbl import RADIO_C
from .rf_radio_d_tbl import RADIO_D

_RADIO = (("a", RADIO_A), ("b", RADIO_B), ("c", RADIO_C), ("d", RADIO_D))


def _rf_write(t, path: str, addr: int, data: int) -> None:
    """[SRC] phy_RFWrite_8814A — addr/data word to the path's LSSI write register."""
    word = (((addr & 0xFF) << 20) | (data & C.RFREG_MASK)) & C.RF_WRITE_MASK
    t.write32(C.RF_LSSI_WRITE[path], word)


def _rf_read(t, path: str, addr: int) -> int:
    """[SRC] phy_RFRead_8814A — RF regs are memory-mapped at base + addr*4."""
    return t.read32(C.RF_READ_BASE[path] + (addr & 0xFF) * 4) & C.RFREG_MASK


def set_rf_masked(t, path: str, addr: int, mask: int, value: int) -> None:
    """[SRC] phy_set_rf_reg — RF read-modify-write (mask != RFREGOFFSETMASK)."""
    if mask == C.RFREG_MASK:
        data = value
    else:
        shift = (mask & -mask).bit_length() - 1
        old = _rf_read(t, path, addr)
        data = (old & ~mask) | ((value << shift) & mask)
    _rf_write(t, path, addr, data)


def _rf_emit(t, path: str):
    """Build the walker's data-row callback for one RF path.

    odm_config_rf_radio_*_8814a -> odm_set_rf_reg writes the LSSI register. A 1 us
    inter-write settle (ODM_delay_us) is omitted — USB control latency already
    dwarfs it — but the 50 ms 0xfe/0xffe settling delays are kept.
    """
    def emit(addr: int, data: int) -> None:
        if addr in C.RF_DELAY_ADDRS:
            time.sleep(50e-3)
            return
        _rf_write(t, path, addr, data)

    return emit


def _config_radio_tables(t, rfe_type: int) -> None:
    """[SRC] phy_RF6052_Config_ParaFile_8814A — radio_a..d via the phy_cond walker."""
    driver1 = build_driver1(rfe_type)
    for path, table in _RADIO:
        walk_table(table, driver1, _rf_emit(t, path))


def _copy_rck1(t) -> None:
    """[SRC] phy_RF6052_Config_ParaFile_8814A — read path-A RCK1, copy to B/C/D."""
    rck1 = _rf_read(t, "a", C.RF_RCK1)
    for path in ("b", "c", "d"):
        _rf_write(t, path, C.RF_RCK1, rck1)


def phy_rf_config(t, rfe_type: int) -> None:
    """[SRC] PHY_RFConfig8814A — per-path RF radio tables + the RCK1 calibration copy.

    The TX-power-tracking table that follows in the vendor function only fills
    software dm arrays (no register I/O), so it has no presence on the wire.
    """
    _config_radio_tables(t, rfe_type)
    _copy_rck1(t)
