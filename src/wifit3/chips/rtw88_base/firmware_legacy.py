"""Legacy MCUFWDL firmware-upload path (family-shared).

Used by rtw88 chips whose wlan CPU is an 8051: 8821a, 8812a, 8723d. The
modern iDDMA path used by 8822b/c and 8814a lives elsewhere (each of those
chips has its own `firmware.py`).

Mirrors the rtw88 kernel functions:
- `wlan_cpu_enable`             (mac.c:440)
- `en_download_firmware_legacy` (mac.c:835)
- `rtw_write_firmware_page`     (usb.c:168)  — block sizes 196 / 8 / 1
- `download_firmware_legacy`    (mac.c:892)  — strips 32-byte legacy header
- `download_firmware_validate_legacy` (mac.c:924) — CPU reset + FW_READY poll

The wire protocol for every register write here is the standard rtw88
vendor control-transfer (bRequest=0x05, bmRequestType=0x40, wValue=addr).
"""

from __future__ import annotations

import logging
import time

from .registers import (
    BIT_FEN_CPUEN,
    BIT_FWDL_CHK_RPT,
    BIT_MCUFWDL_EN,
    BIT_MCUFWDL_RDY,
    BIT_RAM_DL_SEL,
    BIT_ROM_DLEN,
    BIT_ROM_PGE,
    BIT_WINTINI_RDY,
    BIT_WLMCU_IOIF,
    REG_MCUFW_CTRL,
    REG_RSV_CTRL,
    REG_SYS_FUNC_EN,
)
from .transport import Rtw88Transport

logger = logging.getLogger(__name__)


# --- legacy-MCUFWDL constants ----------------------------------------------
FW_HDR_LEGACY_SIZE = 32          # sizeof(struct rtw_fw_hdr_legacy)
FW_START_ADDR_LEGACY = 0x1000    # wValue starts here for each page
DLFW_PAGE_SIZE_LEGACY = 0x1000   # 4096 — one page

# Chunk sizes inside `rtw_usb_write_firmware_page` (non-8723D path).
# (For 8723D the big chunk is 254 instead of 196 — handle if/when needed.)
FW_CHUNK_BIG = 196
FW_CHUNK_MID = 8
FW_CHUNK_SMALL = 1

# FW_READY_LEGACY mask: bits in REG_MCUFW_CTRL that signal the 8051 is
# alive after the CPU reset. = 0xC6.
FW_READY_LEGACY = (
    BIT_MCUFWDL_RDY | BIT_FWDL_CHK_RPT | BIT_WINTINI_RDY | BIT_RAM_DL_SEL
)


# --- wlan CPU enable / disable ---------------------------------------------
def wlan_cpu_enable(transport: Rtw88Transport, enable: bool) -> None:
    """Mirror of `wlan_cpu_enable` (mac.c:440)."""
    if enable:
        transport.write8_set(REG_RSV_CTRL + 1, BIT_WLMCU_IOIF)
        transport.write8_set(REG_SYS_FUNC_EN + 1, BIT_FEN_CPUEN)
    else:
        transport.write8_clr(REG_SYS_FUNC_EN + 1, BIT_FEN_CPUEN)
        transport.write8_clr(REG_RSV_CTRL + 1, BIT_WLMCU_IOIF)


# --- download-mode latch ---------------------------------------------------
def en_download_firmware_legacy(transport: Rtw88Transport, enable: bool) -> None:
    """Mirror of `en_download_firmware_legacy` (mac.c:835).

    On enable: reset 8051, set BIT_MCUFWDL_EN, then clear BIT_ROM_DLEN.
    On disable: clear BIT_MCUFWDL_EN.
    """
    if enable:
        wlan_cpu_enable(transport, False)
        wlan_cpu_enable(transport, True)

        transport.write8_set(REG_MCUFW_CTRL, BIT_MCUFWDL_EN)
        for _ in range(10):
            if transport.read8(REG_MCUFW_CTRL) & BIT_MCUFWDL_EN:
                break
            transport.write8_set(REG_MCUFW_CTRL, BIT_MCUFWDL_EN)
            time.sleep(0.020)
        else:
            raise IOError("MCUFWDL_EN never latched in REG_MCUFW_CTRL")

        transport.write32_clr(REG_MCUFW_CTRL, BIT_ROM_DLEN)
    else:
        transport.write8_clr(REG_MCUFW_CTRL, BIT_MCUFWDL_EN)


# --- page upload -----------------------------------------------------------
def _write_fw_page(transport: Rtw88Transport, page: int,
                   data: bytes, debug_log: bool = False) -> None:
    """Stream one FW page to the chip.

    Sets BIT_ROM_PGE = `page` in REG_MCUFW_CTRL, then issues control-OUT
    transfers (`bRequest=0x05`, `bmRequestType=0x40`) of size 196 → 8 → 1
    to addresses starting at FW_START_ADDR_LEGACY (0x1000).
    """
    transport.write32_mask(REG_MCUFW_CTRL, BIT_ROM_PGE, page)

    addr = FW_START_ADDR_LEGACY
    remaining = len(data)
    offset = 0

    while remaining > 0:
        if remaining >= FW_CHUNK_BIG:
            n = FW_CHUNK_BIG
        elif remaining >= FW_CHUNK_MID:
            n = FW_CHUNK_MID
        else:
            n = FW_CHUNK_SMALL

        chunk = data[offset:offset + n]
        transport.write_block(addr, chunk)
        if debug_log:
            logger.debug("fw page=%d addr=0x%04x n=%d", page, addr, n)
        addr += n
        offset += n
        remaining -= n


def download_firmware_legacy(
    transport: Rtw88Transport,
    fw_bytes: bytes,
    progress_cb=None,
    debug_log: bool = False,
) -> bool:
    """Upload `fw_bytes` (with the 32-byte legacy header) and poll for the ACK.

    Returns True iff `BIT_FWDL_CHK_RPT` reads back set in `REG_MCUFW_CTRL`.

    Caller is responsible for power-on + en_download_firmware_legacy(True)
    before calling, and en_download_firmware_legacy(False) after.
    """
    if len(fw_bytes) <= FW_HDR_LEGACY_SIZE:
        raise ValueError(
            f"firmware blob too short ({len(fw_bytes)}B); expected header + body"
        )
    body = fw_bytes[FW_HDR_LEGACY_SIZE:]
    size = len(body)
    total_pages, tail = divmod(size, DLFW_PAGE_SIZE_LEGACY)

    logger.debug(
        "fw: %d body bytes -> %d full page(s) + %d tail byte(s)",
        size, total_pages, tail,
    )

    # Pre-arm the checksum-report bit. The device flips this back to 1 when
    # the upload + checksum is OK.
    transport.write8_set(REG_MCUFW_CTRL, BIT_FWDL_CHK_RPT)

    for page in range(total_pages):
        chunk = body[page * DLFW_PAGE_SIZE_LEGACY:(page + 1) * DLFW_PAGE_SIZE_LEGACY]
        _write_fw_page(transport, page, chunk, debug_log=debug_log)
        if progress_cb:
            progress_cb(page + 1, total_pages + (1 if tail else 0))

    if tail:
        chunk = body[total_pages * DLFW_PAGE_SIZE_LEGACY:]
        _write_fw_page(transport, total_pages, chunk, debug_log=debug_log)
        if progress_cb:
            progress_cb(total_pages + 1, total_pages + 1)

    # Poll BIT_FWDL_CHK_RPT for up to ~1s (kernel does 10ms; we're slower over USB).
    deadline = time.monotonic() + 1.0
    last_val = 0
    while time.monotonic() < deadline:
        last_val = transport.read8(REG_MCUFW_CTRL)
        if last_val & BIT_FWDL_CHK_RPT:
            logger.debug("fw: BIT_FWDL_CHK_RPT set -> upload ACKed (REG_MCUFW_CTRL=0x%02x)", last_val)
            return True
        time.sleep(0.010)

    logger.error("fw: ACK timeout. Last REG_MCUFW_CTRL = 0x%02x", last_val)
    return False


def download_firmware_validate_legacy(transport: Rtw88Transport) -> tuple[bool, int]:
    """Reset the 8051 and confirm FW is *running*.

    Mirror of `download_firmware_validate_legacy` (mac.c:924). The kernel:
        1. set BIT_MCUFWDL_RDY, clear BIT_WINTINI_RDY in REG_MCUFW_CTRL
        2. toggle the wlan CPU off then on (forces FW to re-init from RAM)
        3. poll until (REG_MCUFW_CTRL & FW_READY_LEGACY) == FW_READY_LEGACY

    Returns (success, last_mcufw_ctrl_value).
    """
    val32 = transport.read32(REG_MCUFW_CTRL)
    val32 |= BIT_MCUFWDL_RDY
    val32 &= ~BIT_WINTINI_RDY
    transport.write32(REG_MCUFW_CTRL, val32 & 0xFFFFFFFF)

    wlan_cpu_enable(transport, False)
    wlan_cpu_enable(transport, True)

    deadline = time.monotonic() + 0.5
    last = 0
    while time.monotonic() < deadline:
        last = transport.read32(REG_MCUFW_CTRL)
        if (last & FW_READY_LEGACY) == FW_READY_LEGACY:
            logger.debug("fw validate: FW_READY_LEGACY satisfied (0x%08x)", last)
            return True, last
        time.sleep(0.020)

    logger.error("fw validate timeout. Last REG_MCUFW_CTRL = 0x%08x "
                 "(needed mask 0x%02x set)", last, FW_READY_LEGACY)
    return False, last


__all__ = [
    "FW_HDR_LEGACY_SIZE",
    "FW_START_ADDR_LEGACY",
    "DLFW_PAGE_SIZE_LEGACY",
    "FW_CHUNK_BIG",
    "FW_CHUNK_MID",
    "FW_CHUNK_SMALL",
    "FW_READY_LEGACY",
    "wlan_cpu_enable",
    "en_download_firmware_legacy",
    "download_firmware_legacy",
    "download_firmware_validate_legacy",
]
