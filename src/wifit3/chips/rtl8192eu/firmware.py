"""RTL8192EU firmware upload + 8051 ready ack.

Cleanroom port of the 8192e fileops firmware path (kernel `8192e.c` +
shared `core.c`):

* `rtl8192eu_load_firmware`      — `8192e.c:640-650` (picks the blob name)
* `rtl8xxxu_reset_8051`          — `core.c:1923-1942` (the SHARED reset)
* `rtl8xxxu_download_firmware`   — `core.c:2004-2104`
* `rtl8xxxu_start_firmware`      — `core.c:1944-2002`
* `rtl8xxxu_firmware_self_reset` — `core.c:2159-2184`

The 8192eu fileops vector (`8192e.c:1721-1767`) selects:

    .load_firmware     = rtl8192eu_load_firmware  ("rtlwifi/rtl8192eu_nic.bin")
    .reset_8051        = rtl8xxxu_reset_8051      (shared, NOT the 8188e's own)
    .writeN_block_size = 128

`.reset_8051` is `rtl8xxxu_reset_8051` (`8192e.c:1728`) rather than the
8188e's `rtl8188eu_reset_8051`: the shared version also toggles bit 0 of
`REG_RSV_CTRL + 1` around the `SYS_FUNC_CPU_ENABLE` clear/set. And unlike
the 8188e vector there is no `.init_reg_hmtfr`, so `start_firmware` skips
the `REG_HMTFR = 0x0f` H2C init (`core.c:1998-1999`).
"""
from __future__ import annotations

import logging
import struct
import time
from importlib.resources import files

from .constants import (
    FW_HEADER_SIZE,
    FW_SIGNATURE_92E,
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
    REG_RSV_CTRL,
    REG_SYS_FUNC,
    RTL_FW_PAGE_SIZE,
    RTL8XXXU_FIRMWARE_POLL_MAX,
    SYS_FUNC_CPU_ENABLE,
)
from .transport import RTL8192EUTransport

logger = logging.getLogger(__name__)

_FIRMWARE_ASSET = "rtl8192eu_nic.bin"


def load_firmware_blob() -> bytes:
    """Load the shipped rtl8192eu_nic.bin (pcap-extracted, byte-verified vs
    `linux-firmware/rtlwifi/rtl8192eu_nic.bin`).

    Returns the full blob *including* the 32-byte
    `struct rtl8xxxu_firmware_header` (`rtl8xxxu.h:908`); the upload payload
    is `len(blob) - 32` (`core.c:2130`).
    """
    asset = files(__package__).joinpath("assets").joinpath(_FIRMWARE_ASSET)
    data = asset.read_bytes()
    if len(data) <= FW_HEADER_SIZE:
        raise ValueError(f"firmware blob too small: {len(data)} bytes")

    signature, = struct.unpack_from("<H", data, 0)
    major,     = struct.unpack_from("<H", data, 4)
    minor      = data[6]
    if (signature & 0xFFF0) != FW_SIGNATURE_92E:
        raise ValueError(
            f"firmware signature 0x{signature:04x} is not 8192eu (expected 0x92e0/family)"
        )
    logger.debug(
        "rtl8192eu_nic.bin: %d bytes, signature=0x%04x, v%d.%d",
        len(data), signature, major, minor,
    )
    return data


# ---- helpers --------------------------------------------------------


def _writeN(t: RTL8192EUTransport, addr: int, buf: bytes) -> None:
    """Mirror of `rtl8xxxu_writeN` (`core.c:826-865`).

    Chunks `buf` into ``FW_WRITE_BLOCK_SIZE`` (128) byte vendor-control
    writes; each chunk advances the wire-side register address by the chunk
    length.
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


def reset_8051(t: RTL8192EUTransport) -> None:
    """Mirror of the shared `rtl8xxxu_reset_8051` (`core.c:1923-1942`).

    Toggles bit 0 of `REG_RSV_CTRL + 1` around the `SYS_FUNC_CPU_ENABLE`
    clear/set; the 8188e's own `rtl8188eu_reset_8051` (`8188e.c:558-568`)
    only touches `REG_SYS_FUNC`.
    """
    val8 = t.read8(REG_RSV_CTRL + 1)
    t.write8(REG_RSV_CTRL + 1, val8 & ~(1 << 0))

    sys_func = t.read16(REG_SYS_FUNC)
    sys_func &= ~SYS_FUNC_CPU_ENABLE
    t.write16(REG_SYS_FUNC, sys_func)

    val8 = t.read8(REG_RSV_CTRL + 1)
    t.write8(REG_RSV_CTRL + 1, val8 | (1 << 0))

    sys_func |= SYS_FUNC_CPU_ENABLE
    t.write16(REG_SYS_FUNC, sys_func)


def firmware_self_reset(t: RTL8192EUTransport) -> None:
    """Mirror of `rtl8xxxu_firmware_self_reset` (`core.c:2159-2184`).

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


def download_firmware(t: RTL8192EUTransport, fw_blob: bytes) -> None:
    """Mirror of `rtl8xxxu_download_firmware` (`core.c:2004-2104`).

    `fw_blob` is the full file (header + payload); only ``[FW_HEADER_SIZE:]``
    is uploaded. The kernel does the same: `priv->fw_size = fw->size -
    sizeof(struct rtl8xxxu_firmware_header)` (`core.c:2130`).
    """
    payload = fw_blob[FW_HEADER_SIZE:]
    fw_size = len(payload)

    # Pre-flight: set REG_SYS_FUNC + 1 |= 4 (FEN_EN bit — the kernel comment
    # is silent on the bit name, but the C does `val8 = read8(SYS_FUNC+1); val8 |= 4`).
    val8 = t.read8(REG_SYS_FUNC + 1)
    t.write8(REG_SYS_FUNC + 1, val8 | 4)

    # 8051 enable
    val16 = t.read16(REG_SYS_FUNC)
    t.write16(REG_SYS_FUNC, val16 | SYS_FUNC_CPU_ENABLE)

    # If FW already running, reset it (core.c:2034-2040).
    if t.read8(REG_MCU_FW_DL) & MCU_FW_RAM_SEL:
        logger.info("firmware is already running, resetting the MCU")
        t.write8(REG_MCU_FW_DL, 0x00)
        reset_8051(t)

    # MCU firmware download enable.
    t.write8(REG_MCU_FW_DL, t.read8(REG_MCU_FW_DL) | MCU_FW_DL_ENABLE)

    # 8051 reset — clear BIT(19) of REG_MCU_FW_DL (core.c:2047-2050).
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
        # `fw_abort` label (core.c:2097-2101).
        val16 = t.read16(REG_MCU_FW_DL)
        t.write16(REG_MCU_FW_DL, val16 & ~MCU_FW_DL_ENABLE)


def start_firmware(t: RTL8192EUTransport) -> None:
    """Mirror of `rtl8xxxu_start_firmware` (`core.c:1944-2002`).

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

    # Set FW_DL_READY, clear WINT_INIT_READY, then reset 8051 so it boots
    # (core.c:1969-1978 — the 8192eu needs this reset or the image never runs).
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
        # Kernel sleeps `udelay(100)` per iteration (core.c:1986).
        time.sleep(0.0001)
    else:
        raise TimeoutError(
            "firmware failed to start (MCU_WINT_INIT_READY never set)"
        )
