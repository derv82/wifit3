"""rtlwifi rtl8192c/fw_common.c: firmware download."""
from __future__ import annotations

import errno
import logging
import time

from . import constants as C
from .efuse import rtl_fill_dummy, rtl_fw_block_write, rtl_fw_page_write
from .mac import IS_NORMAL_CHIP
from .priv import RtlPriv

logger = logging.getLogger(__name__)


def IS_FW_HEADER_EXIST(pfwheader: bytes) -> bool:
    signature = int.from_bytes(pfwheader[0:2], "little")
    return (signature & 0xFFF0) == 0x92C0 or (signature & 0xFFF0) == 0x88C0


def _rtl92c_enable_fw_download(rtlpriv: RtlPriv, enable: bool) -> None:
    """fw_common.c:14."""
    io = rtlpriv.io
    if rtlpriv.rtlhal.hw_type == C.HARDWARE_TYPE_RTL8192CU:
        value32 = io.read32(C.REG_MCUFWDL)
        if enable:
            value32 |= C.MCUFWDL_EN
        else:
            value32 &= ~C.MCUFWDL_EN
        io.write32(C.REG_MCUFWDL, value32)
    elif rtlpriv.rtlhal.hw_type == C.HARDWARE_TYPE_RTL8192CE:
        if enable:
            tmp = io.read8(C.REG_SYS_FUNC_EN + 1)
            io.write8(C.REG_SYS_FUNC_EN + 1, tmp | 0x04)
            tmp = io.read8(C.REG_MCUFWDL)
            io.write8(C.REG_MCUFWDL, tmp | 0x01)
            tmp = io.read8(C.REG_MCUFWDL + 2)
            io.write8(C.REG_MCUFWDL + 2, tmp & 0xf7)
        else:
            tmp = io.read8(C.REG_MCUFWDL)
            io.write8(C.REG_MCUFWDL, tmp & 0xfe)
            io.write8(C.REG_MCUFWDL + 1, 0x00)


def _rtl92c_write_fw(rtlpriv: RtlPriv, version: int, buffer: bytes) -> None:
    """fw_common.c:49."""
    is_version_b = IS_NORMAL_CHIP(version)
    if is_version_b:
        if rtlpriv.rtlhal.hw_type == C.HARDWARE_TYPE_RTL8192CE:
            buffer = rtl_fill_dummy(buffer)
        size = len(buffer)
        pagenums = size // C.FW_8192C_PAGE_SIZE
        remainsize = size % C.FW_8192C_PAGE_SIZE
        if pagenums > 4:
            logger.error("Page numbers should not greater then 4")
        for page in range(pagenums):
            offset = page * C.FW_8192C_PAGE_SIZE
            rtl_fw_page_write(rtlpriv, page, buffer[offset:offset + C.FW_8192C_PAGE_SIZE])
        if remainsize:
            offset = pagenums * C.FW_8192C_PAGE_SIZE
            rtl_fw_page_write(rtlpriv, pagenums, buffer[offset:offset + remainsize])
    else:
        rtl_fw_block_write(rtlpriv, buffer)


def _rtl92c_fw_free_to_go(rtlpriv: RtlPriv) -> int:
    """fw_common.c:89. Returns 0 once the 8051 reports WINTINI_RDY, else -EIO."""
    io = rtlpriv.io
    counter = 0
    while True:
        value32 = io.read32(C.REG_MCUFWDL)
        polling = counter < C.FW_8192C_POLLING_TIMEOUT_COUNT
        counter += 1
        if not polling or value32 & C.FWDL_CHKSUM_RPT:
            break
    if counter >= C.FW_8192C_POLLING_TIMEOUT_COUNT:
        logger.error("chksum report fail! REG_MCUFWDL:0x%08x .", value32)
        return -errno.EIO
    value32 = io.read32(C.REG_MCUFWDL)
    value32 |= C.MCUFWDL_RDY
    value32 &= ~C.WINTINI_RDY
    io.write32(C.REG_MCUFWDL, value32)

    counter = 0
    while True:
        value32 = io.read32(C.REG_MCUFWDL)
        if value32 & C.WINTINI_RDY:
            return 0
        time.sleep(C.FW_8192C_POLLING_DELAY / 1000)
        polling = counter < C.FW_8192C_POLLING_TIMEOUT_COUNT
        counter += 1
        if not polling:
            break
    logger.error("Polling FW ready fail! REG_MCUFWDL:0x%08x.", value32)
    return -errno.EIO


def rtl92c_download_fw(rtlpriv: RtlPriv) -> int:
    """fw_common.c:129. A failed free-to-go is logged, not returned, as in the source."""
    rtlhal = rtlpriv.rtlhal
    version = rtlhal.version
    if rtlhal.pfirmware is None:
        return 1
    pfwdata = rtlhal.pfirmware[:rtlhal.fwsize]
    if IS_FW_HEADER_EXIST(rtlhal.pfirmware):
        rtlhal.fw_version = int.from_bytes(rtlhal.pfirmware[4:6], "little")
        rtlhal.fw_subversion = rtlhal.pfirmware[6]
        pfwdata = pfwdata[C.FW_HEADER_SIZE:]

    _rtl92c_enable_fw_download(rtlpriv, True)
    _rtl92c_write_fw(rtlpriv, version, pfwdata)
    _rtl92c_enable_fw_download(rtlpriv, False)

    if _rtl92c_fw_free_to_go(rtlpriv):
        logger.error("Firmware is not ready to run!")
    return 0
