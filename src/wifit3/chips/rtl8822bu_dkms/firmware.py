"""RTL8822BU firmware — blob loader + WLAN_FW header parse.

The morrownr DKMS driver compiles its firmware in as ``array_mp_8822b_fw_nic``
(v30.20, 161240 B). That blob — NOT the linux-firmware rtw88 one (161176 B, a
different version) — is what the cold captures download, so it is the wire ground
truth and is shipped verbatim in ``assets/rtl8822bu_fw.bin``. The HALMAC iDDMA
download (the register + bulk-OUT sequence that streams it into MCU IMEM/DMEM) is
wired at the firmware-download milestone; the gate then byte-verifies this blob
against the recorded bulk-OUT packets.

[SRC] hal/rtl8822b/hal8822b_fw.c:13389 (array), halmac_fw_88xx.c:115/234 (download),
      halmac_fw_info.h:22-40 (header layout).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .constants import (
    FW_BLOB_SIZE,
    WLAN_FW_HDR_CHKSUM_SIZE,
    WLAN_FW_HDR_DMEM_ADDR,
    WLAN_FW_HDR_DMEM_SIZE,
    WLAN_FW_HDR_EMEM_ADDR,
    WLAN_FW_HDR_EMEM_SIZE,
    WLAN_FW_HDR_IMEM_ADDR,
    WLAN_FW_HDR_IMEM_SIZE,
    WLAN_FW_HDR_MEM_USAGE,
    WLAN_FW_HDR_SIZE,
)

_FW_PATH = Path(__file__).with_name("assets") / "rtl8822bu_fw.bin"


@dataclass
class FwHeader:
    """Parsed WLAN_FW header. Sizes here already include the 8-byte per-segment
    checksum the downloader appends, matching start_dlfw_88xx's arithmetic."""
    dmem_addr: int
    dmem_size: int          # raw dmem + checksum
    imem_addr: int
    imem_size: int          # raw imem + checksum
    emem_addr: int
    emem_size: int          # raw emem + checksum, or 0 when absent


def load_firmware_blob() -> bytes:
    """The 161240-byte morrownr 8822b NIC firmware (wire ground truth)."""
    blob = _FW_PATH.read_bytes()
    if len(blob) != FW_BLOB_SIZE:
        raise ValueError(f"RTL8822BU FW blob is {len(blob)} B, expected {FW_BLOB_SIZE}")
    return blob


def parse_fw_header(blob: bytes) -> FwHeader:
    """start_dlfw_88xx header decode [SRC] halmac_fw_88xx.c:246-257. The MEM_USAGE
    BIT(4) gates emem; each present segment carries an extra 8-byte checksum. The high
    bit of each address is a flag the downloader masks off."""
    def le32(off: int) -> int:
        return struct.unpack_from("<I", blob, off)[0]

    dmem_size = le32(WLAN_FW_HDR_DMEM_SIZE) + WLAN_FW_HDR_CHKSUM_SIZE
    imem_size = le32(WLAN_FW_HDR_IMEM_SIZE) + WLAN_FW_HDR_CHKSUM_SIZE
    emem_size = 0
    if blob[WLAN_FW_HDR_MEM_USAGE] & (1 << 4):
        emem_size = le32(WLAN_FW_HDR_EMEM_SIZE) + WLAN_FW_HDR_CHKSUM_SIZE

    real = WLAN_FW_HDR_SIZE + dmem_size + imem_size + emem_size
    if real != len(blob):
        raise ValueError(f"RTL8822BU FW size {len(blob)} != header-derived {real}")

    return FwHeader(
        dmem_addr=le32(WLAN_FW_HDR_DMEM_ADDR) & ~(1 << 31),
        dmem_size=dmem_size,
        imem_addr=le32(WLAN_FW_HDR_IMEM_ADDR) & ~(1 << 31),
        imem_size=imem_size,
        emem_addr=le32(WLAN_FW_HDR_EMEM_ADDR) & ~(1 << 31),
        emem_size=emem_size,
    )
