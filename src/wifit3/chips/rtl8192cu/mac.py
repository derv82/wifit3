from __future__ import annotations

import errno
import logging

from . import constants as C
from .priv import RtlPriv

logger = logging.getLogger(__name__)


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


def rtl92c_llt_write(rtlpriv: RtlPriv, address: int, data: int) -> bool:
    """rtl8192cu/mac.c:124."""
    io = rtlpriv.io
    status = True
    count = 0
    value = C._LLT_INIT_ADDR(address) | C._LLT_INIT_DATA(data) | C._LLT_OP(C._LLT_WRITE_ACCESS)
    io.write32(C.REG_LLT_INIT, value)
    while True:
        value = io.read32(C.REG_LLT_INIT)
        if C._LLT_OP_VALUE(value) == C._LLT_NO_ACTIVE:
            break
        if count > C.POLLING_LLT_THRESHOLD:
            logger.error("Failed to polling write LLT done at address %d! _LLT_OP_VALUE(%x)",
                         address, C._LLT_OP_VALUE(value))
            status = False
            break
        count += 1
    return status


def rtl92c_init_llt_table(rtlpriv: RtlPriv, boundary: int) -> bool:
    """rtl8192cu/mac.c:154."""
    for i in range(boundary - 1):
        if not rtl92c_llt_write(rtlpriv, i, i + 1):
            logger.error("===> rtl92c_init_llt_table #1 fail")
            return False
    if not rtl92c_llt_write(rtlpriv, boundary - 1, 0xFF):
        logger.error("===> rtl92c_init_llt_table #2 fail")
        return False
    for i in range(boundary, C.LLT_LAST_ENTRY_OF_TX_PKT_BUFFER):
        if not rtl92c_llt_write(rtlpriv, i, i + 1):
            logger.error("===> rtl92c_init_llt_table #3 fail")
            return False
    if not rtl92c_llt_write(rtlpriv, C.LLT_LAST_ENTRY_OF_TX_PKT_BUFFER, boundary):
        logger.error("===> rtl92c_init_llt_table #4 fail")
        return False
    return True


def rtl92c_enable_interrupt(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:331; the 8192CE branch reads rtl_pci, which a USB device never has."""
    io = rtlpriv.io
    rtlusb = rtlpriv.usb
    io.write32(C.REG_HIMR, rtlusb.irq_mask[0] & 0xFFFFFFFF)
    io.write32(C.REG_HIMRE, rtlusb.irq_mask[1] & 0xFFFFFFFF)


def rtl92c_init_interrupt(rtlpriv: RtlPriv) -> None:
    rtl92c_enable_interrupt(rtlpriv)


def rtl92c_disable_interrupt(rtlpriv: RtlPriv) -> None:
    rtlpriv.io.write32(C.REG_HIMR, C.IMR8190_DISABLED)
    rtlpriv.io.write32(C.REG_HIMRE, C.IMR8190_DISABLED)


def rtl92c_init_driver_info_size(rtlpriv: RtlPriv, size: int) -> None:
    rtlpriv.io.write8(C.REG_RX_DRVINFO_SZ, size)


def rtl92c_set_network_type(rtlpriv: RtlPriv, type: int) -> int:
    """rtl8192cu/mac.c:378."""
    if type == C.NL80211_IFTYPE_UNSPECIFIED:
        value = C.NT_NO_LINK
    elif type == C.NL80211_IFTYPE_ADHOC:
        value = C.NT_LINK_AD_HOC
    elif type == C.NL80211_IFTYPE_STATION:
        value = C.NT_LINK_AP
    elif type == C.NL80211_IFTYPE_AP:
        value = C.NT_AS_AP
    else:
        return -errno.EOPNOTSUPP
    rtlpriv.io.write8(C.MSR, value)
    return 0


def rtl92c_init_network_type(rtlpriv: RtlPriv) -> None:
    rtl92c_set_network_type(rtlpriv, C.NL80211_IFTYPE_UNSPECIFIED)


def rtl92c_init_adaptive_ctrl(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:418."""
    io = rtlpriv.io
    value32 = io.read32(C.REG_RRSR)
    value32 &= ~C.RATE_BITMAP_ALL
    value32 |= C.RATE_RRSR_CCK_ONLY_1M
    io.write32(C.REG_RRSR, value32)
    value16 = C._SPEC_SIFS_CCK(0x10) | C._SPEC_SIFS_OFDM(0x10)
    io.write16(C.REG_SPEC_SIFS, value16)
    value16 = C._LRL(0x30) | C._SRL(0x30)
    io.write32(C.REG_RL, value16)


def rtl92c_init_rate_fallback(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:437."""
    io = rtlpriv.io
    io.write32(C.REG_DARFRC, 0x00000000)
    io.write32(C.REG_DARFRC + 4, 0x10080404)
    io.write32(C.REG_RARFRC, 0x04030201)
    io.write32(C.REG_RARFRC + 4, 0x08070605)


def rtl92c_set_cck_sifs(rtlpriv: RtlPriv, trx_sifs: int, ctx_sifs: int) -> None:
    rtlpriv.io.write8(C.REG_SIFS_CCK, trx_sifs)
    rtlpriv.io.write8(C.REG_SIFS_CCK + 1, ctx_sifs)


def rtl92c_set_ofdm_sifs(rtlpriv: RtlPriv, trx_sifs: int, ctx_sifs: int) -> None:
    rtlpriv.io.write8(C.REG_SIFS_OFDM, trx_sifs)
    rtlpriv.io.write8(C.REG_SIFS_OFDM + 1, ctx_sifs)


def rtl92c_init_edca(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:483."""
    io = rtlpriv.io
    value16 = io.read16(C.REG_RD_CTRL)
    value16 |= C.DIS_EDCA_CNT_DWN
    io.write16(C.REG_RD_CTRL, value16)
    rtl92c_set_cck_sifs(rtlpriv, 0xa, 0xa)
    rtl92c_set_ofdm_sifs(rtlpriv, 0xe, 0xe)
    io.write16(C.REG_SIFS_CCK, 0x0a0a)
    io.write16(C.REG_SIFS_OFDM, 0x1010)
    io.write16(C.REG_PROT_MODE_CTRL, 0x0204)
    io.write32(C.REG_BAR_MODE_CTRL, 0x014004)
    io.write32(C.REG_EDCA_BE_PARAM, 0x005EA42B)
    io.write32(C.REG_EDCA_BK_PARAM, 0x0000A44F)
    io.write32(C.REG_EDCA_VI_PARAM, 0x005EA324)
    io.write32(C.REG_EDCA_VO_PARAM, 0x002FA226)
    io.write8(C.REG_PIFS, 0x1C)
    io.write8(C.REG_AGGR_BREAK_TIME, 0x16)
    io.write16(C.REG_NAV_PROT_LEN, 0x0040)
    io.write8(C.REG_BCNDMATIM, 0x02)
    io.write8(C.REG_ATIMWND, 0x02)


def rtl92c_init_ampdu_aggregation(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:515; 0x4CA is unnamed in the source."""
    io = rtlpriv.io
    io.write32(C.REG_AGGLEN_LMT, 0x99997631)
    io.write8(C.REG_AGGR_BREAK_TIME, 0x16)
    io.write16(0x4CA, 0x0708)


def rtl92c_init_beacon_max_error(rtlpriv: RtlPriv) -> None:
    rtlpriv.io.write8(C.REG_BCN_MAX_ERR, 0xFF)


def rtl92c_init_retry_function(rtlpriv: RtlPriv) -> None:
    """rtl8192cu/mac.c:541."""
    io = rtlpriv.io
    value8 = io.read8(C.REG_FWHW_TXQ_CTRL)
    value8 |= C.EN_AMPDU_RTY_NEW
    io.write8(C.REG_FWHW_TXQ_CTRL, value8)
    io.write8(C.REG_ACKTO, 0x40)


def rtl92c_set_min_space(rtlpriv: RtlPriv, is2T: bool) -> None:
    value = C.MAX_MSS_DENSITY_2T if is2T else C.MAX_MSS_DENSITY_1T
    rtlpriv.io.write8(C.REG_AMPDU_MIN_SPACE, value)
