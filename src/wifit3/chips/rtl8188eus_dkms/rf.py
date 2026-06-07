"""RTL8188EUS RF (radio) configuration (M2c).

``PHY_RFConfig8188E`` -> ``PHY_RF6052_Config8188E`` -> ``phy_RF6052_Config_ParaFile``
[SRC] rtl8188e_rf6052.c. This card is 1T1R, so only path A is configured:

    store RFENV control type            (query 0x870[bRFSI_RFENV])
    set RF_ENV enable / output high      (0x860)
    set 3-wire addr/data bit length = 0  (0x824)
    walk array_mp_8188e_radioa           (LSSI write to 0x840)
    restore RFENV control type           (0x870)

Each radio row is ``phy_RFWrite``: DataAndAddr = ((addr<<20) | (data & 0xFFFFF)) &
0x0FFFFFFF, written to the path-A LSSI register; addresses 0xFFE/0xF9..0xFD are
settling delays (no write). [WIRE] cap1 ops 1218.. through the RFENV restore.
"""
from __future__ import annotations

from . import bb, phy_cond
from .constants import (
    b3WireAddressLength,
    b3WireDataLength,
    bRFSI_RFENV,
    RF_DELAY_ADDRS,
    RF_HSSI_PARA2_A,
    RF_INTFE_A,
    RF_INTFO_A,
    RF_INTFS_A,
    RF_LSSI_WRITE_A,
)
from .rf_radio_a_tbl import RADIO_A


def phy_rf_config(t) -> None:
    # Store original RFENV control type (path A).
    rfenv = bb.query_bb_reg(t, RF_INTFS_A, bRFSI_RFENV)
    # Set RF_ENV enable, then output high.
    bb.set_bb_reg(t, RF_INTFE_A, bRFSI_RFENV << 16, 0x1)
    bb.set_bb_reg(t, RF_INTFO_A, bRFSI_RFENV, 0x1)
    # Set 3-wire address (4 bits) and data (12 bits) length selectors to 0.
    bb.set_bb_reg(t, RF_HSSI_PARA2_A, b3WireAddressLength, 0x0)
    bb.set_bb_reg(t, RF_HSSI_PARA2_A, b3WireDataLength, 0x0)
    # Load the radio-A table.
    phy_cond.walk_table(RADIO_A, _emit_rf(t))
    # Restore RFENV control type.
    bb.set_bb_reg(t, RF_INTFS_A, bRFSI_RFENV, rfenv)


def _emit_rf(t):
    def emit(addr: int, data: int) -> None:
        if addr in RF_DELAY_ADDRS:        # settling delay, not a register write
            return
        off = addr & 0xFF
        data_and_addr = ((off << 20) | (data & 0xFFFFF)) & 0x0FFFFFFF
        t.write32(RF_LSSI_WRITE_A, data_and_addr)
    return emit
