"""RTL8821AU firmware upload (legacy MCUFWDL path).

Mirrors the rtw88 kernel functions:
- `en_download_firmware_legacy`  (mac.c:835)
- `download_firmware_legacy`     (mac.c:892)
- `rtw_usb_write_firmware_page`  (usb.c:168)

The wlan CPU is an 8051. Upload protocol is *all* USB control transfers:
addresses start at FW_START_ADDR_LEGACY (0x1000) and advance by chunk size
(196 → 8 → 1 bytes) until each 4096-byte page is written. Between pages the
page index is poked into BIT_ROM_PGE of REG_MCUFW_CTRL.

The device-side ACK that the milestone targets is BIT_FWDL_CHK_RPT in
REG_MCUFW_CTRL — the legacy firmware checksum-OK report bit.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .constants import (
    BIT_FEN_CPUEN,
    BIT_FWDL_CHK_RPT,
    BIT_MCUFWDL_EN,
    BIT_MCUFWDL_RDY,
    BIT_ROM_DLEN,
    BIT_ROM_PGE,
    BIT_WINTINI_RDY,
    BIT_WLMCU_IOIF,
    DLFW_PAGE_SIZE_LEGACY,
    FW_HDR_LEGACY_SIZE,
    FW_READY_LEGACY,
    FW_START_ADDR_LEGACY,
    REG_MCUFW_CTRL,
    REG_RSV_CTRL,
    REG_SYS_FUNC_EN,
)
from .transport import RTL8821AUTransport

logger = logging.getLogger(__name__)

# Chunk sizes inside rtw_usb_write_firmware_page (non-8723D)
FW_CHUNK_BIG = 196
FW_CHUNK_MID = 8
FW_CHUNK_SMALL = 1


def wlan_cpu_enable(transport: RTL8821AUTransport, enable: bool) -> None:
    """Mirror of `wlan_cpu_enable` (mac.c:440)."""
    if enable:
        transport.write8_set(REG_RSV_CTRL + 1, BIT_WLMCU_IOIF)
        transport.write8_set(REG_SYS_FUNC_EN + 1, BIT_FEN_CPUEN)
    else:
        transport.write8_clr(REG_SYS_FUNC_EN + 1, BIT_FEN_CPUEN)
        transport.write8_clr(REG_RSV_CTRL + 1, BIT_WLMCU_IOIF)


def en_download_firmware_legacy(transport: RTL8821AUTransport, enable: bool) -> None:
    """Mirror of `en_download_firmware_legacy` (mac.c:835).

    On enable: reset 8051, set BIT_MCUFWDL_EN, then clear BIT_ROM_DLEN.
    On disable: clear BIT_MCUFWDL_EN.
    """
    if enable:
        wlan_cpu_enable(transport, False)
        wlan_cpu_enable(transport, True)

        transport.write8_set(REG_MCUFW_CTRL, BIT_MCUFWDL_EN)
        for attempt in range(10):
            if transport.read8(REG_MCUFW_CTRL) & BIT_MCUFWDL_EN:
                break
            transport.write8_set(REG_MCUFW_CTRL, BIT_MCUFWDL_EN)
            time.sleep(0.020)
        else:
            raise IOError("MCUFWDL_EN never latched in REG_MCUFW_CTRL")

        transport.write32_clr(REG_MCUFW_CTRL, BIT_ROM_DLEN)
    else:
        transport.write8_clr(REG_MCUFW_CTRL, BIT_MCUFWDL_EN)


def _write_fw_page(transport: RTL8821AUTransport, page: int,
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
    transport: RTL8821AUTransport,
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

    logger.info(
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
            logger.info("fw: BIT_FWDL_CHK_RPT set -> upload ACKed (REG_MCUFW_CTRL=0x%02x)", last_val)
            return True
        time.sleep(0.010)

    logger.error("fw: ACK timeout. Last REG_MCUFW_CTRL = 0x%02x", last_val)
    return False


def load_firmware_blob(path: Path | None = None) -> bytes:
    """Read the canonical FW blob (32B header + body)."""
    if path is None:
        path = Path(__file__).parent / "assets" / "rtw8821a_fw.bin"
    return path.read_bytes()


def download_firmware_validate_legacy(transport: RTL8821AUTransport) -> tuple[bool, int]:
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

    # Kernel polls 10 times with 20ms delay = 200ms budget. We mirror that.
    deadline = time.monotonic() + 0.5
    last = 0
    while time.monotonic() < deadline:
        last = transport.read32(REG_MCUFW_CTRL)
        if (last & FW_READY_LEGACY) == FW_READY_LEGACY:
            logger.info("fw validate: FW_READY_LEGACY satisfied (0x%08x)", last)
            return True, last
        time.sleep(0.020)

    logger.error("fw validate timeout. Last REG_MCUFW_CTRL = 0x%08x "
                 "(needed mask 0x%02x set)", last, FW_READY_LEGACY)
    return False, last
