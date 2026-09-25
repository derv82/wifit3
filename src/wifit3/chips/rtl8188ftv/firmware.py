"""RTL8188FTV firmware upload + 8051 ready ack.

Cleanroom port of three kernel helpers:

* `rtl8xxxu_download_firmware` — `core.c:1984-2084`
* `rtl8xxxu_start_firmware`    — `core.c:1924-1982`
* `rtl8xxxu_firmware_self_reset` — `core.c:2139-2164`

The 8188F fileops vector (`8188f.c:1708-1764`) selects:

    `.load_firmware`    = rtl8188fu_load_firmware   (picks "rtlwifi/rtl8188fufw.bin")
    `.reset_8051`       = rtl8xxxu_reset_8051       (core.c RSV_CTRL dance)
    `.writeN_block_size = 128`

The 8188F needs the RSV_CTRL reset dance (`core.c:1903-1922`): four
register ops (RSV_CTRL+1 clear, SYS_FUNC disable, RSV_CTRL+1 set,
SYS_FUNC enable) instead of the 8188E's simple clear-then-set.

Because `init_reg_hmtfr = 1`, `start_firmware` also writes
`REG_HMTFR = 0x0f` at the end (`core.c:1996-1999`).
"""
from __future__ import annotations

import logging
import struct
import time
from importlib.resources import files

from .constants import (
    FW_HEADER_SIZE,
    FW_SIGNATURE_88F,
    FW_WRITE_BLOCK_SIZE,
    MCU_FW_DL_CSUM_REPORT,
    MCU_FW_DL_ENABLE,
    MCU_FW_DL_READY,
    MCU_FW_RAM_SEL,
    MCU_WINT_INIT_READY,
    REG_FW_START_ADDRESS,
    REG_HMTFR,
    REG_MCU_FW_DL,
    REG_RSV_CTRL,
    REG_SYS_FUNC,
    RTL_FW_PAGE_SIZE,
    RTL8XXXU_FIRMWARE_POLL_MAX,
    SYS_FUNC_CPU_ENABLE,
)
from .transport import RTL8188FTVTransport

logger = logging.getLogger(__name__)

_FIRMWARE_ASSET = "rtl8188fufw.bin"


def load_firmware_blob() -> bytes:
    """Load the shipped rtl8188fufw.bin (pcap-extracted, sha256 verified).

    Returns the full blob *including* the 32-byte
    `struct rtl8xxxu_firmware_header`; payload size is `len(blob) - 32`.
    """
    asset = files(__package__).joinpath("assets").joinpath(_FIRMWARE_ASSET)
    data = asset.read_bytes()
    if len(data) <= FW_HEADER_SIZE:
        raise ValueError(f"firmware blob too small: {len(data)} bytes")

    signature, = struct.unpack_from("<H", data, 0)
    major,     = struct.unpack_from("<H", data, 4)
    minor      = data[6]
    if (signature & 0xFFF0) != FW_SIGNATURE_88F:
        raise ValueError(
            f"firmware signature 0x{signature:04x} is not 8188f (expected 0x88f0/family)"
        )
    logger.debug(
        "rtl8188fufw.bin: %d bytes, signature=0x%04x, v%d.%d",
        len(data), signature, major, minor,
    )
    return data


# ---- helpers --------------------------------------------------------


def _writeN(t: RTL8188FTVTransport, addr: int, buf: bytes) -> None:
    """Mirror of `rtl8xxxu_writeN` (`core.c:830-873`).

    Chunks ``buf`` into ``FW_WRITE_BLOCK_SIZE`` (128) byte vendor-control
    writes.  Each chunk advances both the wire-side register address and
    the source pointer by the chunk length — producing the characteristic
    0x1000/0x1080/0x1100/... ladder the pcap shows for each 4096-byte page.
    """
    blocksize = FW_WRITE_BLOCK_SIZE
    count = len(buf) // blocksize
    remainder = len(buf) % blocksize
    for i in range(count):
        chunk = buf[i * blocksize : (i + 1) * blocksize]
        t.write_block(addr + i * blocksize, chunk)
    if remainder:
        chunk = buf[count * blocksize :]
        t.write_block(addr + count * blocksize, chunk)


def reset_8051(t: RTL8188FTVTransport) -> None:
    """Mirror of `rtl8xxxu_reset_8051` (`core.c:1903-1922`).

    The 8188F fileops selects the **core** reset (not the simple 8188E
    clear+set), which toggles the 8051 through the RSV_CTRL register
    before and after disabling/enabling the CPU clock.
    """
    val8 = t.read8(REG_RSV_CTRL + 1)
    val8 &= ~0x01  # clear BIT(0)
    t.write8(REG_RSV_CTRL + 1, val8)

    sys_func = t.read16(REG_SYS_FUNC)
    sys_func &= ~SYS_FUNC_CPU_ENABLE
    t.write16(REG_SYS_FUNC, sys_func)

    val8 = t.read8(REG_RSV_CTRL + 1)
    val8 |= 0x01  # set BIT(0)
    t.write8(REG_RSV_CTRL + 1, val8)

    sys_func |= SYS_FUNC_CPU_ENABLE
    t.write16(REG_SYS_FUNC, sys_func)


def firmware_self_reset(t: RTL8188FTVTransport) -> None:
    """Mirror of `rtl8xxxu_firmware_self_reset` (`core.c:2139-2164`).

    Tells a running 8051 to reset itself; falls back to brute clearing
    ``SYS_FUNC_CPU_ENABLE`` if the FW doesn't ack within 5 ms.
    """
    t.write8(REG_HMTFR + 3, 0x20)
    for _ in range(100):
        val16 = t.read16(REG_SYS_FUNC)
        if not (val16 & SYS_FUNC_CPU_ENABLE):
            logger.debug("firmware self reset success")
            return
        time.sleep(0.00005)
    # Forced reset
    val16 = t.read16(REG_SYS_FUNC)
    t.write16(REG_SYS_FUNC, val16 & ~SYS_FUNC_CPU_ENABLE)
    logger.warning("firmware self reset timed out — forced 8051 disable")


# ---- the M1 entry points ---------------------------------------------


def download_firmware(t: RTL8188FTVTransport, fw_blob: bytes) -> None:
    """Mirror of `rtl8xxxu_download_firmware` (`core.c:1984-2084`).

    ``fw_blob`` is the full file (header + payload); only
    ``[FW_HEADER_SIZE:]`` is uploaded.  The kernel does the same:
    ``priv->fw_size = fw->size - sizeof(struct rtl8xxxu_firmware_header)``
    (core.c:2110).

    8188F specifics vs 8188E:
    - ``writeN_block_size`` = 128 (not 196), so each 4096-byte page
      ladders across 32 control writes at 0x1000/0x1080/...
    - The pre-8051-enable ``REG_SYS_FUNC+1 |= 4`` step happens here
      (core.c:2004-2011).
    """
    payload = fw_blob[FW_HEADER_SIZE:]
    fw_size = len(payload)

    # Pre-flight: set REG_SYS_FUNC + 1 |= 4  (core.c:2004-2006)
    val8 = t.read8(REG_SYS_FUNC + 1)
    t.write8(REG_SYS_FUNC + 1, val8 | 4)

    # Enable 8051  (core.c:2009-2011)
    val16 = t.read16(REG_SYS_FUNC)
    t.write16(REG_SYS_FUNC, val16 | SYS_FUNC_CPU_ENABLE)

    # If FW already running, reset it (core.c:2014-2020)
    if t.read8(REG_MCU_FW_DL) & MCU_FW_RAM_SEL:
        logger.info("firmware is already running, resetting the MCU")
        t.write8(REG_MCU_FW_DL, 0x00)
        reset_8051(t)

    # MCU firmware download enable (core.c:2023-2025)
    val8 = t.read8(REG_MCU_FW_DL)
    val8 |= MCU_FW_DL_ENABLE
    t.write8(REG_MCU_FW_DL, val8)

    # 8051 reset — clear BIT(19) of REG_MCU_FW_DL (core.c:2028-2030)
    val32 = t.read32(REG_MCU_FW_DL)
    t.write32(REG_MCU_FW_DL, val32 & ~0x0008_0000)

    # Reset firmware-download checksum (core.c:2040-2042)
    val8 = t.read8(REG_MCU_FW_DL)
    val8 |= MCU_FW_DL_CSUM_REPORT
    t.write8(REG_MCU_FW_DL, val8)

    pages = fw_size // RTL_FW_PAGE_SIZE
    remainder = fw_size % RTL_FW_PAGE_SIZE
    logger.info(
        "uploading firmware: %d bytes (%d full pages of %d B + %d B remainder)",
        fw_size, pages, RTL_FW_PAGE_SIZE, remainder,
    )

    try:
        for i in range(pages):
            page_idx = t.read8(REG_MCU_FW_DL + 2) & 0xF8
            t.write8(REG_MCU_FW_DL + 2, page_idx | i)
            start = i * RTL_FW_PAGE_SIZE
            _writeN(t, REG_FW_START_ADDRESS, payload[start : start + RTL_FW_PAGE_SIZE])

        if remainder:
            page_idx = t.read8(REG_MCU_FW_DL + 2) & 0xF8
            t.write8(REG_MCU_FW_DL + 2, page_idx | pages)
            _writeN(t, REG_FW_START_ADDRESS, payload[pages * RTL_FW_PAGE_SIZE:])
    finally:
        # Disable FW download regardless of success (core.c:2079-2081)
        val16 = t.read16(REG_MCU_FW_DL)
        val16 &= ~MCU_FW_DL_ENABLE
        t.write16(REG_MCU_FW_DL, val16)


def start_firmware(t: RTL8188FTVTransport) -> None:
    """Mirror of `rtl8xxxu_start_firmware` (`core.c:1924-1982`).

    Returns silently on success; raises ``TimeoutError`` if either the
    checksum-report poll or the ``MCU_WINT_INIT_READY`` poll times out.

    8188F: because ``fops->init_reg_hmtfr = 1``, this also writes
    ``REG_HMTFR = 0x0f`` (core.c:1996-1999).
    """
    # Poll checksum report (core.c:1937-1941)
    for _ in range(RTL8XXXU_FIRMWARE_POLL_MAX):
        if t.read32(REG_MCU_FW_DL) & MCU_FW_DL_CSUM_REPORT:
            break
    else:
        raise TimeoutError(
            "firmware checksum-report poll timed out (REG_MCU_FW_DL bit 2 never set)"
        )

    # Set FW_DL_READY, clear WINT_INIT_READY (core.c:1949-1952)
    val32 = t.read32(REG_MCU_FW_DL)
    val32 |= MCU_FW_DL_READY
    val32 &= ~MCU_WINT_INIT_READY
    t.write32(REG_MCU_FW_DL, val32)

    # Reset 8051 so it boots (core.c:1958)
    reset_8051(t)

    # Wait for firmware to become ready (core.c:1961-1973)
    for i in range(RTL8XXXU_FIRMWARE_POLL_MAX):
        if t.read32(REG_MCU_FW_DL) & MCU_WINT_INIT_READY:
            logger.debug("MCU_WINT_INIT_READY set after %d polls", i + 1)
            break
        time.sleep(0.0001)
    else:
        raise TimeoutError(
            "firmware failed to start (MCU_WINT_INIT_READY never set)"
        )

    # 8188F: init_reg_hmtfr — write HMTFR  (core.c:1978-1979)
    t.write8(REG_HMTFR, 0x0F)
