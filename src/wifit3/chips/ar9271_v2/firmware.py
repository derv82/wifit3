"""Cold-boot firmware download — port of ath9k_hif_usb_download_fw [SRC] hif_usb.c:1068.

The cold AR9271 enumerates as 0cf3:9271 with no firmware. The host streams the blob into
chip RAM in 4096-byte chunks via vendor control writes (bRequest 0x30), the load address
riding in wValue as ``addr >> 8``, then issues a single "complete" write (bRequest 0x31,
wValue = text-entry >> 8) that makes the chip jump to the downloaded text and re-enumerate.
"""
from __future__ import annotations

from pathlib import Path

from . import constants as C
from .transport import AR9271Transport

_FW_PATH = Path(__file__).parent / "assets" / C.FIRMWARE_NAME


def load_firmware_blob() -> bytes:
    return _FW_PATH.read_bytes()


def download(t: AR9271Transport, fw: bytes) -> None:
    """Stream ``fw`` into chip RAM, then trigger the boot. Mirrors the kernel's while-loop
    exactly: chunk by FW_CHUNK, advance the RAM address by the bytes actually sent, and
    pass ``addr >> 8`` in wValue [SRC] hif_usb.c:1080-1097."""
    addr = C.AR9271_FIRMWARE
    off = 0
    while off < len(fw):
        chunk = fw[off:off + C.FW_CHUNK]
        t.control_out(C.FIRMWARE_DOWNLOAD, addr >> 8, chunk)
        off += len(chunk)
        addr += len(chunk)

    # FW download complete — jump to the text entry point [SRC] hif_usb.c:1108-1111.
    t.control_out(C.FIRMWARE_DOWNLOAD_COMP, C.AR9271_FIRMWARE_TEXT >> 8, None)
