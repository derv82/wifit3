"""Minimal libpcap writer for raw 802.11 frames.

Produces a standard libpcap-format file (NOT pcapng) with linktype
``LINKTYPE_IEEE802_11`` (105). Suitable for handing to ``hcxpcapngtool``
(which accepts both pcap and pcapng) → ``hashcat -m 22000``.
"""

from __future__ import annotations

import struct
import time
import zlib
from pathlib import Path
from typing import Iterable

LINKTYPE_IEEE802_11 = 105
PCAP_MAGIC = 0xA1B2C3D4
PCAP_VERSION = (2, 4)
SNAPLEN = 65535


def _strip_fcs(frame: bytes) -> bytes:
    """Drop a trailing 4-byte 802.11 FCS if (and only if) one is present.

    Self-verifying: strips only when the last 4 bytes are a valid CRC32 of the
    preceding bytes, so a frame that already has no FCS — or whose tail merely
    isn't a CRC — is returned untouched. (A non-FCS tail coinciding with a valid
    CRC32 is a ~2^-32 event.) This is driver-agnostic: some chips deliver RX
    frames with the FCS, some strip it in hardware, and `raw` keeps whatever the
    chip gave us — but saved pcaps are LINKTYPE_IEEE802_11, which readers parse
    as FCS-less, so a retained FCS shows up as a malformed trailing IE.
    """
    if len(frame) < 8:
        return frame
    if zlib.crc32(frame[:-4]) & 0xFFFFFFFF == int.from_bytes(frame[-4:], "little"):
        return frame[:-4]
    return frame


def write_pcap(path: Path, frames: Iterable[bytes]) -> int:
    """Write *frames* (raw 802.11 byte strings) to a pcap at *path*.

    All frames share the current wall-clock timestamp — we don't have
    per-frame capture timestamps preserved in the AP/Handshake model. That's
    fine for hashcat: ``hcxpcapngtool`` doesn't care about timing for PMKID
    or 4-way handshake extraction.

    Returns the number of frames written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("wb") as f:
        f.write(struct.pack(
            "<IHHiIII",
            PCAP_MAGIC,
            PCAP_VERSION[0], PCAP_VERSION[1],
            0,                      # GMT thiszone
            0,                      # sigfigs
            SNAPLEN,
            LINKTYPE_IEEE802_11,
        ))
        now = time.time()
        ts_sec = int(now)
        ts_usec = int((now - ts_sec) * 1_000_000)
        for frame in frames:
            if not frame:
                continue
            frame = _strip_fcs(frame)
            length = len(frame)
            f.write(struct.pack("<IIII", ts_sec, ts_usec, length, length))
            f.write(frame)
            count += 1
    return count
