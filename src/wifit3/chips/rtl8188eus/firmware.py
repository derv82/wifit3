"""RTL8188EUS firmware upload + 8051 ready ack.

Cleanroom port of three kernel helpers:

* `rtl8xxxu_download_firmware` — `core.c:2004`
* `rtl8xxxu_start_firmware`    — `core.c:1944`
* `rtl8xxxu_firmware_self_reset` — `core.c:2159`

The 8188e fileops vector (`8188e.c:1835-1885`) selects:

    `.load_firmware`    = rtl8188eu_load_firmware  (just picks the blob name)
    `.reset_8051`       = rtl8188eu_reset_8051     (clear+set SYS_FUNC_CPU_ENABLE)
    `.writeN_block_size = 196`

So the chip-specific bits boil down to: 196-byte chunked vendor-control
writes, and the simple clear-then-set 8051 reset (`8188e.c:558-568`).
"""
from __future__ import annotations

import logging
import struct
import time
from importlib.resources import files

from .constants import (
    FW_HEADER_SIZE,
    FW_SIGNATURE_88E,
    FW_WRITE_BLOCK_SIZE,
    MCU_FW_DL_8051_RESET_BIT,
    MCU_FW_DL_CSUM_REPORT,
    MCU_FW_DL_ENABLE,
    MCU_FW_DL_READY,
    MCU_FW_RAM_SEL,
    MCU_WINT_INIT_READY,
    REG_FW_START_ADDRESS,
    REG_HMTFR,
    REG_MCU_FW_DL,
    REG_SYS_FUNC,
    RTL_FW_PAGE_SIZE,
    RTL8XXXU_FIRMWARE_POLL_MAX,
    SYS_FUNC_CPU_ENABLE,
)
from .transport import RTL8188EUSTransport

logger = logging.getLogger(__name__)

_FIRMWARE_ASSET = "rtl8188eufw.bin"


def load_firmware_blob() -> bytes:
    """Load the shipped rtl8188eufw.bin (pcap-extracted, byte-verified vs
    `linux-firmware/rtlwifi/rtl8188eufw.bin`).

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
    if (signature & 0xFFF0) != FW_SIGNATURE_88E:
        raise ValueError(
            f"firmware signature 0x{signature:04x} is not 8188e (expected 0x88e0/family)"
        )
    logger.debug(
        "rtl8188eufw.bin: %d bytes, signature=0x%04x, v%d.%d",
        len(data), signature, major, minor,
    )
    return data


# ---- helpers --------------------------------------------------------


def _writeN(t: RTL8188EUSTransport, addr: int, buf: bytes) -> None:
    """Mirror of `rtl8xxxu_writeN` (core.c:826).

    Chunks `buf` into ``FW_WRITE_BLOCK_SIZE`` (196) byte vendor-control
    writes. Each chunk advances both the wire-side register address and
    the source pointer by the chunk length.
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


def reset_8051(t: RTL8188EUSTransport) -> None:
    """Mirror of `rtl8188eu_reset_8051` (8188e.c:558-568)."""
    sys_func = t.read16(REG_SYS_FUNC)
    t.write16(REG_SYS_FUNC, sys_func & ~SYS_FUNC_CPU_ENABLE)
    t.write16(REG_SYS_FUNC, sys_func | SYS_FUNC_CPU_ENABLE)


def firmware_self_reset(t: RTL8188EUSTransport) -> None:
    """Mirror of `rtl8xxxu_firmware_self_reset` (core.c:2159-2184).

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


def download_firmware(t: RTL8188EUSTransport, fw_blob: bytes) -> None:
    """Mirror of `rtl8xxxu_download_firmware` (core.c:2004-2103).

    `fw_blob` is the full file (header + payload); only ``[FW_HEADER_SIZE:]``
    is uploaded. The kernel does the same: `priv->fw_size = fw->size -
    sizeof(struct rtl8xxxu_firmware_header)` (core.c:2130).
    """
    payload = fw_blob[FW_HEADER_SIZE:]
    fw_size = len(payload)

    # Pre-flight: set REG_SYS_FUNC + 1 |= 4 (FEN_EN bit — the kernel comment
    # is silent on which bit name, but the C does `val8 = read8(SYS_FUNC+1); val8 |= 4`).
    val8 = t.read8(REG_SYS_FUNC + 1)
    t.write8(REG_SYS_FUNC + 1, val8 | 4)

    # Enable 8051
    val16 = t.read16(REG_SYS_FUNC)
    t.write16(REG_SYS_FUNC, val16 | SYS_FUNC_CPU_ENABLE)

    # If FW already running, reset it (core.c:2034-2040 + 8188e.c:1212 same check).
    if t.read8(REG_MCU_FW_DL) & MCU_FW_RAM_SEL:
        logger.info("firmware is already running, resetting the MCU")
        t.write8(REG_MCU_FW_DL, 0x00)
        reset_8051(t)

    # MCU firmware download enable.
    t.write8(REG_MCU_FW_DL, t.read8(REG_MCU_FW_DL) | MCU_FW_DL_ENABLE)

    # 8051 reset — clear BIT(19) of REG_MCU_FW_DL.
    val32 = t.read32(REG_MCU_FW_DL)
    t.write32(REG_MCU_FW_DL, val32 & ~MCU_FW_DL_8051_RESET_BIT & 0xFFFFFFFF)

    # Reset firmware-download checksum.
    t.write8(REG_MCU_FW_DL, t.read8(REG_MCU_FW_DL) | MCU_FW_DL_CSUM_REPORT)

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
            _writeN(t, REG_FW_START_ADDRESS, payload[pages * RTL_FW_PAGE_SIZE :])
    finally:
        # Disable FW download regardless of success — matches kernel's
        # `fw_abort` label (core.c:2097-2102).
        val16 = t.read16(REG_MCU_FW_DL)
        t.write16(REG_MCU_FW_DL, val16 & ~MCU_FW_DL_ENABLE)


def start_firmware(t: RTL8188EUSTransport) -> None:
    """Mirror of `rtl8xxxu_start_firmware` (core.c:1944-2002).

    Returns silently on success; raises ``TimeoutError`` if either the
    checksum-report poll or the ``MCU_WINT_INIT_READY`` poll times out.
    """
    # Poll checksum report.
    for _ in range(RTL8XXXU_FIRMWARE_POLL_MAX):
        if t.read32(REG_MCU_FW_DL) & MCU_FW_DL_CSUM_REPORT:
            break
    else:
        raise TimeoutError(
            "firmware checksum-report poll timed out (REG_MCU_FW_DL bit 2 never set)"
        )

    # Set FW_DL_READY, clear WINT_INIT_READY, then reset 8051 so it boots.
    val32 = t.read32(REG_MCU_FW_DL)
    val32 |= MCU_FW_DL_READY
    val32 &= ~MCU_WINT_INIT_READY
    t.write32(REG_MCU_FW_DL, val32 & 0xFFFFFFFF)

    reset_8051(t)

    # Wait for firmware to become ready.
    for i in range(RTL8XXXU_FIRMWARE_POLL_MAX):
        if t.read32(REG_MCU_FW_DL) & MCU_WINT_INIT_READY:
            logger.debug("MCU_WINT_INIT_READY set after %d polls (~%d µs)", i + 1, (i + 1) * 100)
            break
        # Kernel sleeps `udelay(100)` per iteration. asyncio.sleep would be
        # nicer but this function is sync (callable from the bring-up path).
        time.sleep(0.0001)
    else:
        raise TimeoutError(
            "firmware failed to start (MCU_WINT_INIT_READY never set)"
        )
