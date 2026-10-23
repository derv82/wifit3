"""RTL8188FTV DKMS RF serial access + RadioA/TxPowerTrack config (M5b/c).

``serial_write`` / ``serial_read`` / ``set/query_rf_reg`` ported from
``phy_RFSerialWrite_8188F`` / ``phy_RFSerialRead_8188F`` /
``PHY_SetRFReg_8188F`` / ``PHY_QueryRFReg_8188F``
(hal/rtl8188f/rtl8188f_phycfg.c:222-280,330-370,384-446).
``config_rf_reg`` ported from ``odm_ConfigRFReg_8188F``
(hal/phydm/rtl8188f/phydm_regconfig8188f.c:33-90) with the 0xB6/0xB2
readback loops. ``radio_a_config`` walks the extracted RadioA table
(``ODM_ReadAndConfig_MP_8188F_RadioA``); ``rf_config`` ported from
``phy_RF6052_Config_ParaFile`` + ``PHY_RF6052_Config8188F``
(hal/rtl8188f/rtl8188f_rf6052.c:151-290). Path A only on this 1T1R
silicon; ``MaskforPhySet`` is a zero local in the source.
``bRFRegOffsetMask`` is 0xFFFFF (RTL92SE_FPGA_VERIFY != 1).
"""
from __future__ import annotations

import time

from . import bb, phy_cond, rf_radio_a_tbl, rf_txpwr_track_tbl

RF_PATH_A = 0
RF_LSSI_PARAM_A = 0x840
RF_HSSI_PARAM1_A = 0x820
RF_HSSI_PARAM2_A = 0x824
RF_LSSI_READBACK_A = 0x8A0
RF_HSPI_READBACK_A = 0x8B8
RF_REG_OFFSET_MASK = 0xFFFFF
LSSI_READ_ADDRESS = 0x7F800000
LSSI_READ_EDGE = 0x80000000
LSSI_READBACK_DATA = 0xFFFFF

RFINTFS_A = 0x870
RFINTFO_A = 0x860
RFINTFE_A = 0x860
RFSI_RFENV = 0x10
WIRE_ADDR_LEN = 0x400
WIRE_DATA_LEN = 0x800


def serial_write(t, path: int, offset: int, data: int) -> None:
    word = ((offset & 0xFF) << 20) | (data & 0x000FFFFF) & 0x0FFFFFFF
    if path == RF_PATH_A:
        bb.set_bb_reg(t, RF_LSSI_PARAM_A, 0xFFFFFFFF, word)
    else:
        raise ValueError(f"unsupported RF path {path}")


def serial_read(t, path: int, offset: int) -> int:
    if path != RF_PATH_A:
        raise ValueError(f"unsupported RF path {path}")
    offset &= 0xFF
    tmp = bb.query_bb_reg(t, RF_HSSI_PARAM2_A, 0xFFFFFFFF)
    tmp = (tmp & ~LSSI_READ_ADDRESS) | (offset << 23) | LSSI_READ_EDGE
    bb.set_bb_reg(t, RF_HSSI_PARAM2_A, 0xFFFFFFFF, tmp & ~LSSI_READ_EDGE)
    tmp = bb.query_bb_reg(t, RF_HSSI_PARAM2_A, 0xFFFFFFFF)
    bb.set_bb_reg(t, RF_HSSI_PARAM2_A, 0xFFFFFFFF, tmp & ~LSSI_READ_EDGE)
    bb.set_bb_reg(t, RF_HSSI_PARAM2_A, 0xFFFFFFFF, tmp | LSSI_READ_EDGE)
    time.sleep(10e-6)
    time.sleep(50e-6)
    time.sleep(50e-6)
    time.sleep(10e-6)
    pi_enable = bb.query_bb_reg(t, RF_HSSI_PARAM1_A, 0x100)
    if pi_enable:
        return bb.query_bb_reg(t, RF_HSPI_READBACK_A, LSSI_READBACK_DATA)
    # TODO: verify, untested here, needs a card with RF LSSI readback
    return bb.query_bb_reg(t, RF_LSSI_READBACK_A, LSSI_READBACK_DATA)


def set_rf_reg(t, path: int, addr: int, mask: int, data: int) -> None:
    if mask != RF_REG_OFFSET_MASK:
        original = serial_read(t, path, addr)
        shift = bb.bit_shift(mask)
        data = (original & ~mask) | ((data << shift) & 0xFFFFFFFF)
    serial_write(t, path, addr, data)


def query_rf_reg(t, path: int, addr: int, mask: int) -> int:
    return (serial_read(t, path, addr) & mask) >> bb.bit_shift(mask)


def config_rf_reg(t, addr: int, data: int, path: int = RF_PATH_A) -> None:
    if addr in (0xFE, 0xFFE):
        time.sleep(0.050)
        return
    set_rf_reg(t, path, addr, RF_REG_OFFSET_MASK, data)
    time.sleep(1e-6)
    if addr == 0xB6:
        count = 0
        getvalue = query_rf_reg(t, path, addr, 0xFFFFFFFF)
        time.sleep(1e-6)
        while (getvalue >> 8) != (data >> 8):
            count += 1
            set_rf_reg(t, path, addr, RF_REG_OFFSET_MASK, data)
            time.sleep(1e-6)
            getvalue = query_rf_reg(t, path, addr, 0xFFFFFFFF)
            if count > 5:
                break
    if addr == 0xB2:
        count = 0
        getvalue = query_rf_reg(t, path, addr, 0xFFFFFFFF)
        time.sleep(1e-6)
        while getvalue != data:
            count += 1
            set_rf_reg(t, path, addr, RF_REG_OFFSET_MASK, data)
            time.sleep(1e-6)
            set_rf_reg(t, path, 0x18, RF_REG_OFFSET_MASK, 0x0FC07)
            time.sleep(1e-6)
            getvalue = query_rf_reg(t, path, addr, 0xFFFFFFFF)
            if count > 5:
                break


def radio_a_config(t, path: int = RF_PATH_A) -> None:
    phy_cond.walk_table(rf_radio_a_tbl.TABLE,
                        lambda a, v: config_rf_reg(t, a, v, path))


def txpwr_track_tables() -> dict:
    return {k: [list(r) if isinstance(r, list) else r for r in v]
            if isinstance(v, list) and v and isinstance(v[0], list)
            else list(v)
            for k, v in rf_txpwr_track_tbl.TABLES.items()}


def rf_config(t, num_paths: int = 1, load_phy_file: int = 0x44) -> dict:
    for path in range(num_paths):
        saved = bb.query_bb_reg(t, RFINTFS_A, RFSI_RFENV)
        bb.set_bb_reg(t, RFINTFE_A, RFSI_RFENV << 16, 0x1)
        time.sleep(1e-6)
        bb.set_bb_reg(t, RFINTFO_A, RFSI_RFENV, 0x1)
        time.sleep(1e-6)
        bb.set_bb_reg(t, RF_HSSI_PARAM2_A, WIRE_ADDR_LEN, 0x0)
        time.sleep(1e-6)
        bb.set_bb_reg(t, RF_HSSI_PARAM2_A, WIRE_DATA_LEN, 0x0)
        time.sleep(1e-6)
        if path == RF_PATH_A:
            radio_a_config(t, path)
        bb.set_bb_reg(t, RFINTFS_A, RFSI_RFENV, saved)
    if load_phy_file & 0x20:
        # TODO: verify, untested here, needs a host TxPowerTrack para-file
        raise ValueError("TxPowerTrack para-file untested here")
    return txpwr_track_tables()
