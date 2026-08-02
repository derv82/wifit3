"""RTL8812AU firmware upload — thin shim over the family-shared helpers.

The legacy MCUFWDL upload path is identical across rtw88 8051 chips
(8821a, 8812a, 8723d). All upload + validation logic lives in
:mod:`wifit3.chips.rtw88_base.firmware_legacy`. This module just re-exports
that surface under the chip-local name and ships the blob loader.

About the asset blob: `assets/rtw8812a_fw.bin` is the canonical
`linux-firmware/rtw88/rtw8812a_fw.bin` (27030 bytes = 32B legacy header +
26998B body). Body bytes are byte-for-byte identical to what the kernel
uploaded in `driver_captures/captures_rtw88_8812au/capture-1.pcap`.

`download_firmware_legacy` strips the 32B header and uploads the body
exactly as the kernel does.

See `RTL8812AU.md` for the byte-verify proof.
"""

from __future__ import annotations

from pathlib import Path

from wifit3.chips.rtw88_base.firmware_legacy import (
    DLFW_PAGE_SIZE_LEGACY,
    FW_CHUNK_BIG,
    FW_CHUNK_MID,
    FW_CHUNK_SMALL,
    FW_HDR_LEGACY_SIZE,
    FW_READY_LEGACY,
    FW_START_ADDR_LEGACY,
    download_firmware_legacy,
    download_firmware_validate_legacy,
    en_download_firmware_legacy,
    wlan_cpu_enable,
)


def load_firmware_blob(path: Path | None = None) -> bytes:
    """Read the canonical 8812A FW blob (32B header + body)."""
    if path is None:
        path = Path(__file__).parent / "assets" / "rtw8812a_fw.bin"
    return path.read_bytes()


__all__ = [
    "DLFW_PAGE_SIZE_LEGACY",
    "FW_CHUNK_BIG",
    "FW_CHUNK_MID",
    "FW_CHUNK_SMALL",
    "FW_HDR_LEGACY_SIZE",
    "FW_READY_LEGACY",
    "FW_START_ADDR_LEGACY",
    "download_firmware_legacy",
    "download_firmware_validate_legacy",
    "en_download_firmware_legacy",
    "load_firmware_blob",
    "wlan_cpu_enable",
]
