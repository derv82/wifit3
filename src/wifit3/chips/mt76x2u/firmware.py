"""MT76x2U firmware upload — ROM patch + main FW (ILM + DLM).

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

Mirrors:
  - driver_sources/mt76-source-v6.18/mt76x2/usb_mcu.c       (mt76x2u_mcu_fw_init)
  - driver_sources/mt76-source-v6.18/mt76x02_usb_mcu.c      (__mt76x02u_mcu_fw_send_data,
                                                         mt76x02u_mcu_fw_reset)

ROM-patch semaphore: `rom_protect = !is_mt7612(dev)`. On the reference 0x7612
(WiFi-only) `is_mt7612` is true, so `rom_protect` is false and the
MT_MCU_SEMAPHORE_03 acquire/release dance is skipped — the structural reason
MT7612U doesn't hit the patch-semaphore wall that paused MT7921AU. Any other
mt76x2 strap the driver claims (e.g. the MT7662U `0e8d:7632`, a WiFi+BT combo)
reports chip != 0x7612, so `rom_protect` is true and the ROM patch must hold
MT_MCU_SEMAPHORE_03 against the on-chip BT core. `is_mt7612` is passed in from
the runtime ASIC-version read. [SRC] mt76x2/usb_mcu.c:59,65-70,138-139.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from pathlib import Path

from .constants import (
    DLM_SIZE,
    EP_OUT_INBAND_CMD,
    ILM_SIZE,
    MCU_FW_URB_MAX_PAYLOAD,
    MCU_ROM_PATCH_MAX_PAYLOAD,
    MT76U_MCU_DLM_OFFSET,
    MT76U_MCU_DLM_OFFSET_E3,
    MT76U_MCU_ILM_OFFSET,
    MT76U_MCU_ROM_PATCH_OFFSET,
    MT76XX_REV_E3,
    MT_FCE_DMA_ADDR,
    MT_FCE_DMA_LEN,
    MT_FCE_PDMA_GLOBAL_CONF,
    MT_FCE_PSE_CTRL,
    MT_FCE_SKIP_FS,
    MT_MCU_CLOCK_CTL,
    MT_MCU_COM_REG0,
    MT_MCU_SEMAPHORE_03,
    MT_TX_CPU_FROM_FCE_BASE_PTR,
    MT_TX_CPU_FROM_FCE_CPU_DESC_IDX,
    MT_TX_CPU_FROM_FCE_MAX_COUNT,
    MT_USB_DMA_CFG_RX_BULK_AGG_TOUT,
    MT_USB_DMA_CFG_RX_BULK_EN,
    MT_USB_DMA_CFG_TX_BULK_EN,
    MT_USB_U3DMA_CFG,
    MT_VEND_TYPE_CFG,
    ROM_PATCH_BODY_SIZE,
)
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)

# MCU message header bitfields (4-byte little-endian).
# [SRC] driver_sources/mt76-source-v6.18/mt76x02_dma.h:32
_MT_MCU_MSG_TYPE_CMD = 1 << 30
_MT_MCU_MSG_PORT_SHIFT = 27
_CPU_TX_PORT = 2  # enum dma_msg_port

# Polling defaults (kernel uses 10 ms ticks; mt76_poll_msec=msec / 10 iters).
_POLL_TICK_MS = 10

_ASSETS = Path(__file__).parent / "assets"


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _fw_info_header(chunk_len: int) -> int:
    """Build the 4-byte mt76 MCU info that prefixes each FW chunk."""
    return (
        _MT_MCU_MSG_TYPE_CMD
        | (_CPU_TX_PORT << _MT_MCU_MSG_PORT_SHIFT)
        | (chunk_len & 0xFFFF)
    )


def _build_fw_chunk_frame(chunk: bytes) -> bytes:
    """Wrap a FW chunk for the bulk-OUT path: [info][chunk][4B zero pad]."""
    info = _fw_info_header(len(chunk))
    return struct.pack("<I", info) + chunk + b"\x00\x00\x00\x00"


async def _poll_reg32_msec(transport: MT76x2UTransport, addr: int,
                           mask: int, expected: int, timeout_ms: int) -> bool:
    """Poll `addr` every 10 ms until `(value & mask) == expected` or timeout."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        val = transport.read32(addr)
        if (val & mask) == expected:
            return True
        if time.monotonic() >= deadline:
            logger.error(
                "poll timeout on 0x%04x: last=0x%08x mask=0x%08x want=0x%08x",
                addr, val, mask, expected,
            )
            return False
        await asyncio.sleep(_POLL_TICK_MS / 1000)


def _enable_usb_dma_cfg(transport: MT76x2UTransport) -> None:
    """Enable bulk DMA on the chip-side USB-3 DMA controller.

    [SRC] mt76x2/usb_mcu.c:99 — same writes in both ROM-patch and main-FW
    paths. The address sits on the CFG bus so it uses bReq=0x46.
    """
    val = (
        MT_USB_DMA_CFG_RX_BULK_EN
        | MT_USB_DMA_CFG_TX_BULK_EN
        | (0x20 & MT_USB_DMA_CFG_RX_BULK_AGG_TOUT)
    )
    transport.write32(MT_VEND_TYPE_CFG | MT_USB_U3DMA_CFG, val)


def _program_fce(transport: MT76x2UTransport) -> None:
    """Program the FCE (Frontend Cmd Engine) before each FW upload section.

    Replicates the 5 register writes that follow each `fw_reset`:
      [SRC] mt76x2/usb_mcu.c:108-117
    """
    transport.write32(MT_FCE_PSE_CTRL, 0x1)
    transport.write32(MT_TX_CPU_FROM_FCE_BASE_PTR, 0x400230)
    transport.write32(MT_TX_CPU_FROM_FCE_MAX_COUNT, 0x1)
    transport.write32(MT_FCE_PDMA_GLOBAL_CONF, 0x44)
    transport.write32(MT_FCE_SKIP_FS, 0x3)


def _fw_reset(transport: MT76x2UTransport) -> None:
    """Vendor reset (mt76x02u_mcu_fw_reset).

    bmRequestType=0x40 (vendor OUT), bRequest=0x01, wValue=0x01, no payload.
    [SRC] mt76x02_usb_mcu.c:207
    """
    transport.vendor_dev_mode(0x0001)


async def _send_fw_chunks(
    transport: MT76x2UTransport, body: bytes,
    dst_base: int, max_payload: int,
    label: str,
) -> bool:
    """Send a FW body in chunks of up to `max_payload-8` data bytes each.

    Mirrors `mt76x02u_mcu_fw_send_data` from kernel C:
      - max_len = max_payload - 8
      - For each chunk:
          single_wr(MT_FCE_DMA_ADDR, dst_base + offset)
          single_wr(MT_FCE_DMA_LEN,  (len << 16))
          bulk-OUT([info | chunk | 4B zero pad])
          read MT_TX_CPU_FROM_FCE_CPU_DESC_IDX, increment, write back
          sleep 5-10 ms
    """
    max_len = max_payload - 8
    total = len(body)
    pos = 0
    chunk_idx = 0
    while pos < total:
        clen = min(max_len, total - pos)
        chunk = body[pos:pos + clen]
        dst = dst_base + pos

        transport.single_wr_fce(MT_FCE_DMA_ADDR, dst)
        # Kernel rounds len up to 4. Our pcap-derived chunk sizes are
        # already 4-byte aligned (verified: 2040, 14584, and section
        # remainders 1840 / 5152 / 2844). Apply roundup defensively.
        rounded = (clen + 3) & ~3
        transport.single_wr_fce(MT_FCE_DMA_LEN, rounded << 16)

        frame = _build_fw_chunk_frame(chunk)
        try:
            written = await transport.async_write_bulk(EP_OUT_INBAND_CMD, frame,
                                                       timeout_ms=1000)
        except Exception as e:
            logger.error("%s chunk %d: bulk write failed: %s",
                         label, chunk_idx, e)
            return False
        if written != len(frame):
            logger.error("%s chunk %d: short write %d/%d",
                         label, chunk_idx, written, len(frame))
            return False

        # Increment FCE CPU desc index — the kernel does this after each chunk.
        try:
            idx_val = transport.read32(MT_TX_CPU_FROM_FCE_CPU_DESC_IDX)
            transport.write32(MT_TX_CPU_FROM_FCE_CPU_DESC_IDX, (idx_val + 1) & 0xFFFFFFFF)
        except Exception as e:
            logger.error("%s chunk %d: FCE desc-idx update failed: %s",
                         label, chunk_idx, e)
            return False

        await asyncio.sleep(0.005)  # kernel: usleep_range(5000, 10000)
        chunk_idx += 1
        pos += clen

    logger.debug("%s: uploaded %d chunks, %d bytes", label, chunk_idx, total)
    return True


# ---------------------------------------------------------------------------
# ROM-patch helpers (mt76x2u_mcu_enable_patch / mt76x2u_mcu_reset_wmt).
# Opaque vendor payloads — copied verbatim from kernel C.
# ---------------------------------------------------------------------------
_ENABLE_PATCH_PAYLOAD = bytes([
    0x6f, 0xfc, 0x08, 0x01,
    0x20, 0x04, 0x00, 0x00,
    0x00, 0x09, 0x00,
])
_RESET_WMT_PAYLOAD = bytes([
    0x6f, 0xfc, 0x05, 0x01,
    0x07, 0x01, 0x00, 0x04,
])


def _enable_patch(transport: MT76x2UTransport) -> None:
    """[SRC] mt76x2/usb_mcu.c:28 — vendor request, class type (0x20|OUT)."""
    transport.dev.ctrl_transfer(
        bmRequestType=0x20,           # USB_DIR_OUT | USB_TYPE_CLASS
        bRequest=0x01,                # MT_VEND_DEV_MODE
        wValue=0x0012,
        wIndex=0,
        data_or_wLength=_ENABLE_PATCH_PAYLOAD,
        timeout=transport.timeout_ms,
    )


def _reset_wmt(transport: MT76x2UTransport) -> None:
    """[SRC] mt76x2/usb_mcu.c:43 — also class type."""
    transport.dev.ctrl_transfer(
        bmRequestType=0x20,
        bRequest=0x01,
        wValue=0x0012,
        wIndex=0,
        data_or_wLength=_RESET_WMT_PAYLOAD,
        timeout=transport.timeout_ms,
    )


def _load_ivb(transport: MT76x2UTransport) -> None:
    """[SRC] mt76x2/usb_mcu.c:21 — vendor type (0x40|OUT), wValue=0x12, NO payload."""
    transport.vendor_dev_mode(0x0012)


# ---------------------------------------------------------------------------
# Asset loading.
# ---------------------------------------------------------------------------
def _load_asset(name: str, expected_size: int) -> bytes:
    p = _ASSETS / name
    blob = p.read_bytes()
    if len(blob) != expected_size:
        raise RuntimeError(
            f"FW asset size mismatch: {p} is {len(blob)} bytes, "
            f"expected {expected_size}"
        )
    return blob


# ---------------------------------------------------------------------------
# Top-level upload routines.
# ---------------------------------------------------------------------------
async def load_rom_patch(transport: MT76x2UTransport, asic_rev: int,
                         is_mt7612: bool = True) -> bool:
    """Upload the MT7662 ROM patch.

    Returns True on success (including "already applied"). [SRC] mt76x2/usb_mcu.c:57.

    ``rom_protect = !is_mt7612`` — on non-0x7612 mt76x2 combo silicon the ROM
    patch shares MT_MCU_SEMAPHORE_03 with the on-chip BT core and must hold it
    across the load. The reference 0x7612 (``is_mt7612=True``) has no BT
    contender, so the acquire/release is skipped and its path is byte-identical.
    [SRC] mt76x2/usb_mcu.c:59,65-70,138-139.
    """
    rom_protect = not is_mt7612
    if rom_protect and not await _poll_reg32_msec(
        transport, MT_MCU_SEMAPHORE_03, 1, 1, timeout_ms=600
    ):
        logger.error("could not get hardware semaphore for ROM patch")
        return False

    rev_e3 = asic_rev >= MT76XX_REV_E3
    patch_reg = MT_MCU_CLOCK_CTL if rev_e3 else MT_MCU_COM_REG0
    patch_mask = 1 << 0 if rev_e3 else 1 << 1

    # Idempotency check — re-upload is harmless but slower. The kernel's
    # rom_protect path returns here still holding the semaphore
    # ([SRC] usb_mcu.c:80-83); wifit3 only reaches this on the cold path, where
    # the patch reg is never hot, so the early-out is effectively dead code.
    cur = transport.read32(patch_reg)
    if cur & patch_mask:
        logger.info("ROM patch already applied (reg 0x%04x = 0x%08x)", patch_reg, cur)
        return True

    blob = _load_asset("mt7662_rom_patch_body.bin", ROM_PATCH_BODY_SIZE)

    _enable_usb_dma_cfg(transport)
    _fw_reset(transport)
    await asyncio.sleep(0.008)  # kernel: usleep_range(5000, 10000)

    _program_fce(transport)

    ok = await _send_fw_chunks(
        transport, blob,
        dst_base=MT76U_MCU_ROM_PATCH_OFFSET,
        max_payload=MCU_ROM_PATCH_MAX_PAYLOAD,
        label="ROM patch",
    )
    if not ok:
        if rom_protect:
            transport.write32(MT_MCU_SEMAPHORE_03, 1)   # release [SRC] usb_mcu.c:139
        return False

    _enable_patch(transport)
    _reset_wmt(transport)
    await asyncio.sleep(0.020)  # kernel: mdelay(20)

    applied = await _poll_reg32_msec(transport, patch_reg, patch_mask, patch_mask,
                                     timeout_ms=100)
    if rom_protect:
        transport.write32(MT_MCU_SEMAPHORE_03, 1)   # release [SRC] usb_mcu.c:139
    if not applied:
        logger.error("ROM patch failed to apply (reg 0x%04x never went hot)",
                     patch_reg)
        return False
    logger.info("ROM patch applied (reg 0x%04x bit set).", patch_reg)
    return True


async def load_main_firmware(transport: MT76x2UTransport, asic_rev: int) -> bool:
    """Upload ILM + DLM, trigger IVB, poll FW-running. [SRC] mt76x2/usb_mcu.c:144."""
    ilm = _load_asset("mt7662_ilm.bin", ILM_SIZE)
    dlm = _load_asset("mt7662_dlm.bin", DLM_SIZE)
    dlm_offset = (MT76U_MCU_DLM_OFFSET_E3 if asic_rev >= MT76XX_REV_E3
                  else MT76U_MCU_DLM_OFFSET)

    _fw_reset(transport)
    await asyncio.sleep(0.008)

    _enable_usb_dma_cfg(transport)
    _program_fce(transport)

    if not await _send_fw_chunks(
        transport, ilm,
        dst_base=MT76U_MCU_ILM_OFFSET,
        max_payload=MCU_FW_URB_MAX_PAYLOAD,
        label="ILM",
    ):
        return False
    if not await _send_fw_chunks(
        transport, dlm,
        dst_base=dlm_offset,
        max_payload=MCU_FW_URB_MAX_PAYLOAD,
        label="DLM",
    ):
        return False

    _load_ivb(transport)
    if not await _poll_reg32_msec(transport, MT_MCU_COM_REG0, 1 << 0, 1 << 0,
                                  timeout_ms=100):
        logger.error("Firmware failed to start (MT_MCU_COM_REG0 BIT(0) never set)")
        return False

    # Latch FW-running ack so the chip knows the host saw it.
    # [SRC] mt76x2/usb_mcu.c:224
    transport.rmw32(MT_MCU_COM_REG0, 1 << 1, 1 << 1)
    # Re-enable FCE for in-band MCU commands.
    transport.write32(MT_FCE_PSE_CTRL, 0x1)
    logger.info("MT7612U firmware is running.")
    return True


async def upload_firmware(transport: MT76x2UTransport, asic_rev: int,
                          is_mt7612: bool = True) -> bool:
    """Run the full 2-stage upload: ROM patch then main FW.

    ``is_mt7612`` gates the ROM-patch semaphore (see ``load_rom_patch``);
    defaults to the reference 0x7612 (semaphore skipped)."""
    if not await load_rom_patch(transport, asic_rev, is_mt7612=is_mt7612):
        return False
    return await load_main_firmware(transport, asic_rev)
