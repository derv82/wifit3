"""rt2870.bin firmware loader.

Ports ``rt2800_load_firmware`` (rt2800lib.c:714-793) + the per-chip
``rt2800usb_write_firmware`` (rt2800usb.c:210-265) for the USB transport.

Wire sequence (rt5392 / rt5592 / rt3572 — anything that isn't
RT2860/RT2872/RT3070):

  1. write32(AUTOWAKEUP_CFG, 0)               wake the chip's FW power island
  2. wait_csr_ready                           MAC_CSR0 readable + non-zero
  3. disable_wpdma                            clear TX/RX DMA enable + busy bits
  4. write_multi(FIRMWARE_IMAGE_BASE,         upload 4 KB of fw_bytes
                 fw_bytes[offset:offset+4096])  (offset=4096 in 8K-blobs,
                                                 0 in our 4K-blob)
  5. write32(H2M_MAILBOX_CID, 0xFFFFFFFF)
  6. write32(H2M_MAILBOX_STATUS, 0xFFFFFFFF)
  7. vendor_request(USB_DEVICE_MODE,          chip starts executing FW
                    wIndex=0, wValue=USB_MODE_FIRMWARE)
  8. msleep(10)
  9. write32(H2M_MAILBOX_CSR, 0)
 10. poll PBF_SYS_CTRL.READY until set
 11. disable_wpdma (again)
 12. write32(H2M_BBP_AGENT, 0)
 13. write32(H2M_MAILBOX_CSR, 0)
 14. write32(H2M_INT_SRC, 0)
 15. mcu_request(MCU_BOOT_SIGNAL, 0, 0, 0)
 16. msleep(1)

Firmware blob shape:
  * linux-firmware ships ``rt2870.bin`` as **8192 bytes** (two 4-KB halves:
    first half for RT2860/RT2872 PCI, second half for USB chips).
  * Our local ``assets/rt5572.bin`` is **4096 bytes** — already the USB
    half (offset 4096 has been pre-stripped). For 4K blobs we use
    ``offset=0``; for 8K blobs we use ``offset=4096``.
  * CRC16-CCITT covers all but the last 2 bytes (the embedded CRC).
"""
from __future__ import annotations

import logging
import time

from .constants import (
    AUTOWAKEUP_CFG,
    FIRMWARE_IMAGE_BASE,
    H2M_BBP_AGENT,
    H2M_INT_SRC,
    H2M_MAILBOX_CID,
    H2M_MAILBOX_CSR,
    H2M_MAILBOX_STATUS,
    MCU_BOOT_SIGNAL,
    PBF_SYS_CTRL,
    PBF_SYS_CTRL_READY,
    REGISTER_BUSY_COUNT,
    REGISTER_TIMEOUT_FIRMWARE_MS,
    USB_DEVICE_MODE,
    USB_MODE_FIRMWARE,
    USB_VENDOR_REQUEST_OUT,
)
from .transport import RT5572Transport

logger = logging.getLogger(__name__)

FW_CHUNK_LEN = 4096


# ----------------------------------------------------------------------
# CRC-CCITT (Linux ``crc_ccitt`` from lib/crc-ccitt.c — LSB-first,
# reflected polynomial 0x8408, init 0xFFFF).  NOT the MSB-first
# "CCITT-FALSE / XModem" variant — they look similar but produce
# different results.  Matches the kernel
# ``crc_ccitt(~0, data, len-2)`` + ``swab16(crc)`` pattern in
# rt2800_check_firmware_crc.
# ----------------------------------------------------------------------
def _crc_ccitt(data: bytes) -> int:
    """Compute Linux-style CRC-CCITT (init 0xFFFF, reversed poly
    0x8408) over ``data``."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408
            else:
                crc >>= 1
    return crc


def check_firmware_crc(blob_chunk: bytes) -> bool:
    """Verify the trailing 2-byte CRC of a 4096-byte FW chunk.

    Mirrors ``rt2800_check_firmware_crc`` (rt2800lib.c:628-658).  Kernel
    uses kernel-internal crc_ccitt + swab16 — we compute CRC-CCITT then
    compare against the byte-swapped trailer ``(data[-2] << 8 | data[-1])``.
    """
    expected = (blob_chunk[-2] << 8) | blob_chunk[-1]
    crc = _crc_ccitt(blob_chunk[:-2])
    # kernel does: crc = swab16(crc); return fw_crc == crc;
    # swab16(x) = ((x & 0xff) << 8) | (x >> 8)
    crc_swapped = ((crc & 0xFF) << 8) | (crc >> 8)
    return expected == crc_swapped


# ----------------------------------------------------------------------
# WPDMA disable — mirrors rt2800_disable_wpdma (rt2800lib.c:589-601)
# ----------------------------------------------------------------------
def disable_wpdma(t: RT5572Transport) -> None:
    from .constants import (
        WPDMA_GLO_CFG,
        WPDMA_GLO_CFG_ENABLE_RX_DMA,
        WPDMA_GLO_CFG_ENABLE_TX_DMA,
        WPDMA_GLO_CFG_RX_DMA_BUSY,
        WPDMA_GLO_CFG_TX_DMA_BUSY,
        WPDMA_GLO_CFG_TX_WRITEBACK_DONE,
    )
    reg = t.read32(WPDMA_GLO_CFG)
    reg &= ~(
        WPDMA_GLO_CFG_ENABLE_TX_DMA
        | WPDMA_GLO_CFG_TX_DMA_BUSY
        | WPDMA_GLO_CFG_ENABLE_RX_DMA
        | WPDMA_GLO_CFG_RX_DMA_BUSY
    )
    reg |= WPDMA_GLO_CFG_TX_WRITEBACK_DONE
    t.write32(WPDMA_GLO_CFG, reg & 0xFFFFFFFF)


# ----------------------------------------------------------------------
# wait_csr_ready — poll MAC_CSR0 until non-zero (rt2800lib.c:549-563)
# ----------------------------------------------------------------------
def wait_csr_ready(t: RT5572Transport) -> bool:
    from .constants import MAC_CSR0
    for _ in range(REGISTER_BUSY_COUNT):
        reg = t.read32(MAC_CSR0)
        if reg and reg != 0xFFFFFFFF:
            return True
        time.sleep(0.001)
    return False


# ----------------------------------------------------------------------
# MCU request — port of rt2800_mcu_request (rt2800lib.c:515-547) +
# WAIT_FOR_MCU (rt2800lib.c:62-65) which polls H2M_MAILBOX_CSR_OWNER.
# ----------------------------------------------------------------------
def _wait_for_mcu(t: RT5572Transport) -> bool:
    """Poll H2M_MAILBOX_CSR until the OWNER byte (top 8 bits) is 0
    (i.e. the MCU has consumed the previous request).  Returns True on
    success, False if the MCU never released."""
    from .constants import H2M_MAILBOX_CSR_OWNER
    for _ in range(REGISTER_BUSY_COUNT):
        reg = t.read32(H2M_MAILBOX_CSR)
        if not (reg & H2M_MAILBOX_CSR_OWNER):
            return True
        time.sleep(0.001)
    return False


def mcu_request(
    t: RT5572Transport,
    command: int,
    token: int = 0,
    arg0: int = 0,
    arg1: int = 0,
) -> None:
    """Send a single MCU command via the H2M_MAILBOX_CSR doorbell."""
    from .constants import HOST_CMD_CSR
    if not _wait_for_mcu(t):
        raise IOError("MCU busy — H2M_MAILBOX_CSR_OWNER never cleared")

    reg = (
        (1 << 24)                            # OWNER = host
        | ((token & 0xFF) << 16)
        | ((arg1 & 0xFF) << 8)
        | (arg0 & 0xFF)
    )
    t.write32(H2M_MAILBOX_CSR, reg)
    t.write32(HOST_CMD_CSR, command & 0xFF)


# ----------------------------------------------------------------------
# Kick the chip into FW-execution mode (USB_MODE_FIRMWARE vendor xfer)
# ----------------------------------------------------------------------
def kick_firmware_mode(t: RT5572Transport) -> None:
    """vendor_request(bRequest=USB_DEVICE_MODE, wIndex=0, wValue=USB_MODE_FIRMWARE).

    Kernel uses REGISTER_TIMEOUT_FIRMWARE_MS (1000 ms) here because the
    chip may sit on the request while it initializes its 8051.
    """
    t.dev.ctrl_transfer(
        USB_VENDOR_REQUEST_OUT,
        USB_DEVICE_MODE,
        USB_MODE_FIRMWARE,                    # wValue
        0,                                    # wIndex
        b"",
        REGISTER_TIMEOUT_FIRMWARE_MS,
    )


# ----------------------------------------------------------------------
# Top-level loader
# ----------------------------------------------------------------------
def load_firmware(
    t: RT5572Transport,
    fw_bytes: bytes,
    *,
    silicon_id: int,
    progress_cb=None,
) -> None:
    """Upload rt2870.bin firmware to the chip.

    Raises IOError on any wait-loop timeout.  Returns silently on success.

    Args:
        fw_bytes: the rt2870.bin contents. Either 4096 bytes (USB half
            only, as our local ``rt5572.bin`` ships) or a multiple of
            4096 bytes (full linux-firmware blob — we use offset 4096
            for USB chips).
        silicon_id: the MAC_CSR0 chipset value (used to decide which
            section of the FW blob to upload).
    """
    from .constants import RT_RT3070

    if progress_cb:
        progress_cb(0.00, "Verifying firmware CRC")

    if len(fw_bytes) < FW_CHUNK_LEN:
        raise IOError(f"firmware too short: {len(fw_bytes)} < {FW_CHUNK_LEN}")
    if len(fw_bytes) % FW_CHUNK_LEN != 0:
        raise IOError(
            f"firmware length {len(fw_bytes)} is not a multiple of {FW_CHUNK_LEN}"
        )

    # Pick the right 4-KB section. RT2860/RT2872/RT3070 (legacy chips)
    # use section 0; everything else uses section 1.
    if silicon_id == RT_RT3070 or len(fw_bytes) == FW_CHUNK_LEN:
        # Single-section blob (our local rt5572.bin pre-stripped) or
        # legacy chip → upload from offset 0.
        offset = 0
    else:
        offset = FW_CHUNK_LEN

    chunk = fw_bytes[offset:offset + FW_CHUNK_LEN]
    if not check_firmware_crc(chunk):
        raise IOError(
            "firmware CRC mismatch — blob may be corrupt or for a "
            "different chip family"
        )
    logger.debug(
        "firmware: %d-byte blob, using section @offset=%d, CRC OK",
        len(fw_bytes), offset,
    )

    if progress_cb:
        progress_cb(0.10, "Resetting chip pre-FW")

    # Unconditional in the kernel — issued above the is_pci block (which guards only
    # AUX_CTRL/PWR_PIN_CFG), so it is NOT PCI-only. [SRC] rt2800lib.c:731
    t.write32(AUTOWAKEUP_CFG, 0)
    if not wait_csr_ready(t):
        raise IOError("wait_csr_ready timeout — chip never returned a valid MAC_CSR0")
    disable_wpdma(t)

    if progress_cb:
        progress_cb(0.20, f"Uploading firmware ({FW_CHUNK_LEN} bytes)")

    # rt2800usb_write_firmware opens with an autorun_detect: an AutoRun NIC boots
    # FW from its own flash, so the host skips the RAM upload. Our dongles report
    # not-autorun, so the upload proceeds — but the probe op is on the wire.
    # [SRC] rt2800usb.c:210-244.
    if t.autorun_detect():
        logger.info("NIC in AutoRun mode, skipping FW RAM upload")
    else:
        # Stream the FW chunk into chip RAM at FIRMWARE_IMAGE_BASE.
        t.write_multi(FIRMWARE_IMAGE_BASE, chunk)

    if progress_cb:
        progress_cb(0.60, "Triggering FW execution (USB_MODE_FIRMWARE)")

    t.write32(H2M_MAILBOX_CID, 0xFFFFFFFF)
    t.write32(H2M_MAILBOX_STATUS, 0xFFFFFFFF)
    kick_firmware_mode(t)
    time.sleep(0.010)
    t.write32(H2M_MAILBOX_CSR, 0)

    if progress_cb:
        progress_cb(0.75, "Waiting for PBF_SYS_CTRL.READY")

    # Poll for PBF.READY — kernel uses REGISTER_BUSY_COUNT (100) with
    # 1 ms sleeps.
    for _ in range(REGISTER_BUSY_COUNT):
        reg = t.read32(PBF_SYS_CTRL)
        if reg & PBF_SYS_CTRL_READY:
            break
        time.sleep(0.001)
    else:
        raise IOError(
            "PBF_SYS_CTRL.READY never set — FW did not boot. "
            "Possible causes: wrong firmware for this chip family, "
            "or chip needs a replug."
        )

    if progress_cb:
        progress_cb(0.90, "Sending MCU_BOOT_SIGNAL")

    # Final init shim per rt2800_load_firmware tail.
    disable_wpdma(t)
    t.write32(H2M_BBP_AGENT, 0)
    t.write32(H2M_MAILBOX_CSR, 0)
    t.write32(H2M_INT_SRC, 0)
    mcu_request(t, MCU_BOOT_SIGNAL, token=0, arg0=0, arg1=0)
    time.sleep(0.001)

    if progress_cb:
        progress_cb(1.00, "Firmware booted")


def load_firmware_blob() -> bytes:
    """Read the bundled firmware blob from assets/rt5572.bin."""
    from pathlib import Path
    fw_path = Path(__file__).parent / "assets" / "rt5572.bin"
    return fw_path.read_bytes()
