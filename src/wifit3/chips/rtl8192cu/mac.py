from __future__ import annotations

from . import constants as C
from .priv import RtlPriv


def CHIP_BONDING_IDENTIFIER(value: int) -> int:
    return (value >> 22) & 0x3


def IS_NORMAL_CHIP(version: int) -> bool:
    return bool(version & C.NORMAL_CHIP)


def IS_2T2R(version: int) -> bool:
    return (version & C.RF_TYPE_MASK) == C.CHIP_92C_BITMASK


def IS_92C_SERIAL(version: int) -> bool:
    """fw_common.h:34; false for a 1T2R version (RF type bits 0b11)."""
    return IS_2T2R(version)


def IS_92C_1T2R(version: int) -> bool:
    return (version & C.CHIP_92C_1T2R) == C.CHIP_92C_1T2R


def IS_VENDOR_UMC(version: int) -> bool:
    return bool(version & C.CHIP_VENDOR_UMC)


def IS_VENDOR_UMC_A_CUT(version: int) -> bool:
    return IS_VENDOR_UMC(version) and not (version & C.CUT_VERSION_MASK)


def IS_81XXC_VENDOR_UMC_B_CUT(version: int) -> bool:
    return IS_VENDOR_UMC(version) and (version & C.CUT_VERSION_MASK) == C.CHIP_VENDOR_UMC_B_CUT


def rtl92c_read_chip_version(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:27."""
    rtlhal = rtlpriv.rtlhal
    rtlphy = rtlpriv.phy
    value32 = rtlpriv.io.read32(C.REG_SYS_CFG)
    if value32 & C.TRP_VAUX_EN:
        chip_version = C.VERSION_TEST_CHIP_92C if value32 & C.TYPE_ID else C.VERSION_TEST_CHIP_88C
    else:
        chip_version = C.NORMAL_CHIP
        chip_version |= C.CHIP_92C if value32 & C.TYPE_ID else 0
        chip_version |= C.CHIP_VENDOR_UMC if value32 & C.VENDOR_ID else 0
        if IS_VENDOR_UMC(chip_version):
            chip_version |= C.CHIP_VENDOR_UMC_B_CUT if value32 & C.CHIP_VER_RTL_MASK else 0
        if IS_92C_SERIAL(chip_version):
            value32 = rtlpriv.io.read32(C.REG_HPON_FSM)
            chip_version |= (C.CHIP_92C_1T2R
                             if CHIP_BONDING_IDENTIFIER(value32) == C.CHIP_BONDING_92C_1T2R else 0)
    rtlhal.version = chip_version

    if IS_92C_SERIAL(rtlhal.version):
        rtlphy.rf_type = C.RF_1T2R if IS_92C_1T2R(rtlhal.version) else C.RF_2T2R
    else:
        rtlphy.rf_type = C.RF_1T1R
    if rtlphy.rf_type == C.RF_1T1R:
        rtlpriv.dm.rfpath_rxenable[0] = True
    else:
        rtlpriv.dm.rfpath_rxenable[0] = rtlpriv.dm.rfpath_rxenable[1] = True
