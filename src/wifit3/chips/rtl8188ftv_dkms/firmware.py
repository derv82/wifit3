"""RTL8188FTV DKMS firmware download + ready (M4).

Ported from ``rtl8188f_FirmwareDownload`` + ``_FWDownloadEnable`` +
``_BlockWrite``/``_PageWrite``/``_WriteFW`` + ``_8051Reset8188`` +
``polling_fwdl_chksum`` + ``_FWFreeToGo``
(hal/rtl8188f/rtl8188f_hal_init.c:28-66,95-263,265-303,304-374,1081-1270).
Only the ``#if 1`` IO-write path is on the graph; the ``#else`` TX-packet
path compiles out. The fwdl test-fail triggers default to false
(core/rtw_debug.c:288-309). Surprise-removal checks guard no wire ops: a failed
write returns False, a failed read returns zeros, and the transport raises ENODEV
once the device is marked removed.
"""
from __future__ import annotations

import logging
import time

from . import constants as C

logger = logging.getLogger(__name__)


def BIT(n: int) -> int:
    return 1 << n


def reset_8051(t) -> None:
    io_rst = t.read8(C.REG_RSV_CTRL + 1)
    t.write8(C.REG_RSV_CTRL + 1, io_rst & ~BIT(0))
    cpu_rst = t.read8(C.REG_SYS_FUNC_EN + 1)
    t.write8(C.REG_SYS_FUNC_EN + 1, cpu_rst & ~BIT(2))
    io_rst = t.read8(C.REG_RSV_CTRL + 1)
    t.write8(C.REG_RSV_CTRL + 1, io_rst | BIT(0))
    cpu_rst = t.read8(C.REG_SYS_FUNC_EN + 1)
    t.write8(C.REG_SYS_FUNC_EN + 1, cpu_rst | BIT(2))


def download_enable(t, enable: bool) -> None:
    if enable:
        tmp = t.read8(C.REG_SYS_FUNC_EN + 1)
        t.write8(C.REG_SYS_FUNC_EN + 1, tmp | 0x04)
        tmp = t.read8(C.REG_MCUFWDL)
        t.write8(C.REG_MCUFWDL, tmp | 0x01)
        count = 0
        while True:
            tmp = t.read8(C.REG_MCUFWDL)
            if tmp & 0x01:
                break
            t.write8(C.REG_MCUFWDL, tmp | 0x01)
            time.sleep(0.001)
            count += 1
            if count >= 100:
                break
        tmp = t.read8(C.REG_MCUFWDL + 2)
        t.write8(C.REG_MCUFWDL + 2, tmp & 0xF7)
    else:
        tmp = t.read8(C.REG_MCUFWDL)
        t.write8(C.REG_MCUFWDL, tmp & 0xFE)


def block_write(t, buf: bytes) -> bool:
    size = len(buf)
    count_p1, remain_p1 = divmod(size, C.FW_BLOCK_P1)
    for i in range(count_p1):
        off = i * C.FW_BLOCK_P1
        if not t.writeN(C.FW_8188F_START_ADDRESS + off, buf[off:off + C.FW_BLOCK_P1]):
            logger.warning("_BlockWrite phase 1 i:%d", i)  # printk, :109-112
            return False
    if remain_p1:
        off = count_p1 * C.FW_BLOCK_P1
        count_p2, remain_p2 = divmod(remain_p1, C.FW_BLOCK_P2)
        for i in range(count_p2):
            o = off + i * C.FW_BLOCK_P2
            if not t.writeN(C.FW_8188F_START_ADDRESS + o, buf[o:o + C.FW_BLOCK_P2]):
                return False
        if remain_p2:
            off = count_p1 * C.FW_BLOCK_P1 + count_p2 * C.FW_BLOCK_P2
            for i in range(remain_p2):
                if not t.write8(C.FW_8188F_START_ADDRESS + off + i, buf[off + i]):
                    logger.warning("_BlockWrite phase 3 i:%d", i)  # printk, :170-173
                    return False
    return True


def page_write(t, page: int, buf: bytes) -> bool:
    t.write8(C.REG_MCUFWDL + 2, (t.read8(C.REG_MCUFWDL + 2) & 0xF8) | (page & 0x07))
    return block_write(t, buf)


def write_fw(t, buf: bytes) -> bool:
    pages, remain = divmod(len(buf), C.MAX_DLFW_PAGE_SIZE)
    for page in range(pages):
        off = page * C.MAX_DLFW_PAGE_SIZE
        if not page_write(t, page, buf[off:off + C.MAX_DLFW_PAGE_SIZE]):
            return False
    if remain:
        off = pages * C.MAX_DLFW_PAGE_SIZE
        return page_write(t, pages, buf[off:])
    return True


def polling_fwdl_chksum(t, min_cnt: int, timeout_ms: int) -> bool:
    start = time.monotonic()
    cnt = 0
    while True:
        cnt += 1
        value32 = t.read32(C.REG_MCUFWDL)
        if value32 & C.FWDL_ChkSum_rpt:
            break
        time.sleep(0)
        if not ((time.monotonic() - start) * 1000 < timeout_ms or cnt < min_cnt):
            break
    return bool(value32 & C.FWDL_ChkSum_rpt)


def fw_free_to_go(t, min_cnt: int, timeout_ms: int) -> bool:
    expected = C.MCUFWDL_RDY | C.FWDL_ChkSum_rpt | C.WINTINI_RDY | C.RAM_DL_SEL
    value32 = t.read32(C.REG_MCUFWDL)
    t.write32(C.REG_MCUFWDL, (value32 | C.MCUFWDL_RDY) & ~C.WINTINI_RDY)
    reset_8051(t)
    start = time.monotonic()
    cnt = 0
    while True:
        cnt += 1
        value32 = t.read32(C.REG_MCUFWDL)
        if (value32 & expected) == expected:
            break
        time.sleep(0)
        if not ((time.monotonic() - start) * 1000 < timeout_ms or cnt < min_cnt):
            break
    return (value32 & expected) == expected


def init_firmware_vars(t) -> None:
    t.write8(C.REG_HMETFR, 0x0F)


_FIRMWARE_ASSET = "rtl8188fufw.bin"


def load_firmware_blob() -> bytes:
    from importlib.resources import files
    data = files(__package__).joinpath("assets").joinpath(_FIRMWARE_ASSET).read_bytes()
    if len(data) <= 32:
        raise ValueError(f"firmware blob too small: {len(data)} bytes")
    version, subversion, signature, _ = parse_header(data)
    if (version, subversion, signature) != (4, 0, 0x88F1):
        raise ValueError(f"unexpected firmware header: {(version, subversion, hex(signature))}")
    return data


def parse_header(blob: bytes) -> tuple[int, int, int, bytes]:
    signature = int.from_bytes(blob[0:2], "little")
    version = int.from_bytes(blob[4:6], "little")
    subversion = int.from_bytes(blob[6:8], "little")
    if (signature & 0xFFF0) == 0x88F0:
        return version, subversion, signature, blob[32:]
    return version, subversion, signature, blob


def download_firmware(t, blob: bytes) -> tuple[int, int, int]:
    if len(blob) > C.FW_8188F_SIZE:
        raise ValueError(f"firmware size {len(blob)} exceeds {C.FW_8188F_SIZE}")
    version, subversion, signature, payload = parse_header(blob)
    tmp = t.read8(0xA3)
    t.write8(0xA3, (tmp & 0xF8) | 0x02)
    t.read8(0xA0) & 0x03
    if t.read8(C.REG_MCUFWDL) & C.RAM_DL_SEL:
        t.write8(C.REG_MCUFWDL, 0x00)
        reset_8051(t)
    download_enable(t, True)
    start = time.monotonic()
    write_fw_count = 0
    while write_fw_count < 3 or (time.monotonic() - start) < 0.5:
        write_fw_count += 1
        t.write8(C.REG_MCUFWDL, t.read8(C.REG_MCUFWDL) | C.FWDL_ChkSum_rpt)
        if not write_fw(t, payload):
            continue
        if polling_fwdl_chksum(t, 5, 50):
            break
    download_enable(t, False)
    try:
        success = fw_free_to_go(t, 10, 200)  # :1253, whatever the checksum poll said
        if not success:
            value8 = t.read8(C.REG_MCUFWDL)
            t.write8(C.REG_MCUFWDL, value8 & ~BIT(0) & ~BIT(1))
            raise IOError("firmware download failed")
        return version, subversion, signature
    finally:
        init_firmware_vars(t)
