"""RTL8188EUS firmware download to FW-ready (M1) — vendor-faithful port.

Mirrors ``rtl8188e_FirmwareDownload`` [SRC] hal/rtl8188e/rtl8188e_hal_init.c:859
and its helpers. The 8188e uploads the FW blob over EP0 as wide vendor control
writes (rtw_writeN) into the FW SRAM window at FW_8188E_START_ADDRESS, page by
page (4 KB), each page split into 196-byte / 8-byte / 1-byte control writes
(``_BlockWrite``). FW-ready is ``_FWFreeToGo`` polling WINTINI_RDY in REG_MCUFWDL.

The blob (``assets/rtl8188eufw.bin``) is the vendor ``array_mp_8188e_t_fw_nic[]``
verbatim — byte-identical to linux-firmware ``rtl8188eufw.bin`` (SHA256 match).
Every register access matches the vendor read/write width and ordering and
reproduces the cold-boot capture byte-for-byte (``scripts/rtl8188eus_dkms/
verify_pcap.py``). [WIRE] cap1 frames 1195..1681 (ops 552..795).
"""
from __future__ import annotations

import time
from pathlib import Path

from .constants import (
    BIT,
    FW_8188E_START_ADDRESS,
    FW_HEADER_SIZE,
    FW_SIGNATURE_88E,
    FW_SIGNATURE_MASK,
    FWDL_ChkSum_rpt,
    MAX_DLFW_PAGE_SIZE,
    MAX_REG_BLOCK_SIZE,
    MCUFWDL_RDY,
    RAM_DL_SEL,
    REG_MCUFWDL,
    REG_RSV_CTRL,
    REG_SYS_FUNC_EN,
    WINTINI_RDY,
)
from .pwrseq import power_on

_FW_BLOB = Path(__file__).resolve().parent / "assets" / "rtl8188eufw.bin"


def load_firmware_blob() -> bytes:
    return _FW_BLOB.read_bytes()


# --- 8051 / MCU-IO reset [SRC] rtl8188e_hal_init.c _MCUIO_Reset88E / _8051Reset88E
def _mcuio_reset(t, reset: bool) -> None:
    u = t.read8(REG_RSV_CTRL)
    t.write8(REG_RSV_CTRL, u & ~BIT(1))
    u = t.read8(REG_RSV_CTRL + 1)
    if reset:
        t.write8(REG_RSV_CTRL + 1, u & ~BIT(3))
    else:
        t.write8(REG_RSV_CTRL + 1, u | BIT(3))


def _8051_reset(t) -> None:
    _mcuio_reset(t, True)
    u = t.read8(REG_SYS_FUNC_EN + 1)
    t.write8(REG_SYS_FUNC_EN + 1, u & ~BIT(2))
    _mcuio_reset(t, False)
    t.write8(REG_SYS_FUNC_EN + 1, u | BIT(2))   # cached u, no re-read (per vendor)


# --- FW download enable/disable [SRC] _FWDownloadEnable_8188E
def _fw_download_enable(t, enable: bool) -> None:
    if enable:
        t.write8(REG_MCUFWDL, t.read8(REG_MCUFWDL) | 0x01)         # MCU FW DL enable
        t.write8(REG_MCUFWDL + 2, t.read8(REG_MCUFWDL + 2) & 0xF7)  # 8051 reset
    else:
        t.write8(REG_MCUFWDL, t.read8(REG_MCUFWDL) & 0xFE)         # MCU FW DL disable
        t.write8(REG_MCUFWDL + 1, 0x00)                            # reserved for fw ext


# --- block / page / image write [SRC] _BlockWrite / _PageWrite / _WriteFW
def _block_write(t, buf: bytes) -> None:
    """USB phase #1 196-byte, phase #2 8-byte, phase #3 1-byte control writes,
    each to FW_8188E_START_ADDRESS + running offset."""
    n1, rem1 = divmod(len(buf), MAX_REG_BLOCK_SIZE)
    for i in range(n1):
        off = i * MAX_REG_BLOCK_SIZE
        t.write_block(FW_8188E_START_ADDRESS + off, buf[off:off + MAX_REG_BLOCK_SIZE])
    if rem1:
        base = n1 * MAX_REG_BLOCK_SIZE
        n2, rem2 = divmod(rem1, 8)
        for i in range(n2):
            off = base + i * 8
            t.write_block(FW_8188E_START_ADDRESS + off, buf[off:off + 8])
        if rem2:
            base2 = base + n2 * 8
            for i in range(rem2):
                t.write8(FW_8188E_START_ADDRESS + base2 + i, buf[base2 + i])


def _page_write(t, page: int, buf: bytes) -> None:
    v = (t.read8(REG_MCUFWDL + 2) & 0xF8) | (page & 0x07)
    t.write8(REG_MCUFWDL + 2, v)
    _block_write(t, buf)


def _write_fw(t, buf: bytes) -> None:
    pages, remain = divmod(len(buf), MAX_DLFW_PAGE_SIZE)
    for page in range(pages):
        off = page * MAX_DLFW_PAGE_SIZE
        _page_write(t, page, buf[off:off + MAX_DLFW_PAGE_SIZE])
    if remain:
        off = pages * MAX_DLFW_PAGE_SIZE
        _page_write(t, pages, buf[off:])


# --- FW-ready polling [SRC] polling_fwdl_chksum / _FWFreeToGo
def _polling_chksum(t) -> bool:
    for _ in range(5000):
        if t.read32(REG_MCUFWDL) & FWDL_ChkSum_rpt:
            return True
        time.sleep(10e-6)
    return False


def _fw_free_to_go(t) -> bool:
    v = t.read32(REG_MCUFWDL)
    v |= MCUFWDL_RDY
    v &= ~WINTINI_RDY
    t.write32(REG_MCUFWDL, v)
    _8051_reset(t)
    for _ in range(5000):
        if t.read32(REG_MCUFWDL) & WINTINI_RDY:
            return True
        time.sleep(10e-6)
    return False


def download_firmware(t, blob: bytes) -> None:
    """``rtl8188e_FirmwareDownload`` — strip the 32-byte header, then enable DL,
    write the image, and wait for FW-ready."""
    sig = int.from_bytes(blob[0:2], "little")
    buf = blob[FW_HEADER_SIZE:] if (sig & FW_SIGNATURE_MASK) == FW_SIGNATURE_88E else blob

    if t.read8(REG_MCUFWDL) & RAM_DL_SEL:   # 8051 running RAM code -> reset first
        t.write8(REG_MCUFWDL, 0x00)
        _8051_reset(t)

    _fw_download_enable(t, True)
    for _ in range(3):   # vendor retries up to 3x / 500 ms until chksum OK
        t.write8(REG_MCUFWDL, t.read8(REG_MCUFWDL) | FWDL_ChkSum_rpt)  # reset chksum
        _write_fw(t, buf)
        if _polling_chksum(t):
            break
    _fw_download_enable(t, False)
    if not _fw_free_to_go(t):
        raise RuntimeError("RTL8188EUS: firmware not ready (WINTINI_RDY timeout)")


def bring_up(t, blob: bytes | None = None) -> None:
    """M1 runtime entry: power on the MAC, then download firmware to FW-ready.

    (On hardware the probe-phase efuse read runs between power_on and the FW
    download — see ``efuse.py`` — but the FW download itself is self-contained.)
    """
    if blob is None:
        blob = load_firmware_blob()
    power_on(t)
    download_firmware(t, blob)
