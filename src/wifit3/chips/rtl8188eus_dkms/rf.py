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
    bLSSIReadAddress,
    bLSSIReadBackData,
    bLSSIReadEdge,
    bMaskDWord,
    bRFSI_RFENV,
    RF_CHNLBW,
    RF_DELAY_ADDRS,
    RF_HSSI_PARA1_A,
    RF_HSSI_PARA1_B,
    RF_HSSI_PARA2_A,
    RF_HSSI_PARA2_B,
    RF_INTFE_A,
    RF_INTFO_A,
    RF_INTFS_A,
    RF_LSSI_READBACK_A,
    RF_LSSI_READBACK_B,
    RF_LSSI_READBACK_PI_A,
    RF_LSSI_READBACK_PI_B,
    RF_LSSI_WRITE_A,
    RF_PI_ENABLE,
    RFREGOFFSETMASK,
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


# --- RF register read (the 3-wire LSSI read path) -------------------------
# Per-path BB register addresses used by the serial read, indexed [path A, path B]
# (phy_InitBBRFRegisterDefinition). This card is 1T1R, but hal_init reads both paths.
_RF_HSSI_PARA2 = (RF_HSSI_PARA2_A, RF_HSSI_PARA2_B)              # 0x824 / 0x82c
_RF_HSSI_PARA1 = (RF_HSSI_PARA1_A, RF_HSSI_PARA1_B)             # 0x820 / 0x828
_RF_LSSI_READBACK = (RF_LSSI_READBACK_A, RF_LSSI_READBACK_B)    # 0x8a0 / 0x8a4
_RF_LSSI_READBACK_PI = (RF_LSSI_READBACK_PI_A, RF_LSSI_READBACK_PI_B)  # 0x8b8 / 0x8bc


def _phy_rf_serial_read(t, path: int, offset: int) -> int:
    """``phy_RFSerialRead`` [SRC] rtl8188e_phycfg.c:434 — read one RF register over
    the 3-wire LSSI bus. Stage the read offset into HSSI parameter2 (clear the read
    edge on path-A's copy, then drive ``offset<<23 | read-edge`` on the path's copy),
    then read back the 20-bit value from the LSSI read-back register — the parallel
    (PI) variant when that path's parallel interface is enabled, else the serial one.
    The read offset always rides path-A's HSSI parameter2 (vendor quirk: only 0x824
    drives the read trigger for both paths)."""
    offset &= 0xFF
    tmplong = bb.query_bb_reg(t, RF_HSSI_PARA2_A, bMaskDWord)
    tmplong2 = tmplong if path == 0 else bb.query_bb_reg(t, _RF_HSSI_PARA2[path], bMaskDWord)
    tmplong2 = (tmplong2 & ~bLSSIReadAddress) | (offset << 23) | bLSSIReadEdge
    bb.set_bb_reg(t, RF_HSSI_PARA2_A, bMaskDWord, tmplong & ~bLSSIReadEdge)
    bb.set_bb_reg(t, _RF_HSSI_PARA2[path], bMaskDWord, tmplong2)
    rf_pi_enable = bb.query_bb_reg(t, _RF_HSSI_PARA1[path], RF_PI_ENABLE)
    src = _RF_LSSI_READBACK_PI[path] if rf_pi_enable else _RF_LSSI_READBACK[path]
    return bb.query_bb_reg(t, src, bLSSIReadBackData)


def phy_query_rf_reg(t, path: int, addr: int, mask: int) -> int:
    """``PHY_QueryRFReg8188E`` — masked RF register read."""
    val = _phy_rf_serial_read(t, path, addr)
    shift = (mask & -mask).bit_length() - 1
    return (val & mask) >> shift


def read_rf_chnl_val(t) -> tuple[int, int]:
    """Cache RfRegChnlVal[0]/[1] (the RF channel/BW register, paths A+B) at the end
    of hal_init [SRC] usb_halinit.c:1539. RfRegChnlVal[0] is the base the channel-tune
    path RMWs to switch channel later, so the bring-up must read it now."""
    return (phy_query_rf_reg(t, 0, RF_CHNLBW, RFREGOFFSETMASK),
            phy_query_rf_reg(t, 1, RF_CHNLBW, RFREGOFFSETMASK))


# --- RF register write (the 3-wire LSSI write path) -----------------------
def phy_rf_serial_write(t, path: int, addr: int, data: int) -> None:
    """``phy_RFSerialWrite`` [SRC] rtl8188e_phycfg.c:556 — write one RF register over
    the 3-wire LSSI bus: DataAndAddr = (addr<<20 | data) & 0x0FFFFFFF -> the path's
    LSSI-write register (path A 0x840; this card is 1T1R so only path A is used)."""
    data_and_addr = (((addr & 0xFF) << 20) | (data & RFREGOFFSETMASK)) & 0x0FFFFFFF
    t.write32(RF_LSSI_WRITE_A, data_and_addr)


def set_rf_reg(t, path: int, addr: int, mask: int, data: int) -> None:
    """``PHY_SetRFReg8188E`` [SRC] rtl8188e_phycfg.c:688 — masked RF write. A partial
    mask first serial-reads the register and merges; a full-width mask writes directly."""
    if mask != RFREGOFFSETMASK:
        orig = _phy_rf_serial_read(t, path, addr)
        shift = (mask & -mask).bit_length() - 1
        data = (orig & ~mask) | ((data << shift) & mask)
    phy_rf_serial_write(t, path, addr, data)
