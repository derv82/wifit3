"""Minimal libpcap writer for raw 802.11 frames.

Produces a standard libpcap-format file (NOT pcapng) with linktype
``LINKTYPE_IEEE802_11`` (105). Suitable for handing to ``hcxpcapngtool``
(which accepts both pcap and pcapng) → ``hashcat -m 22000``.
"""

from __future__ import annotations

import struct
import time
from pathlib import Path
from typing import Iterable, Iterator

from wifit3.crack.handshake import handshake_messages
from wifit3.models import AccessPoint
from wifit3.persist.config import Config
from wifit3.persist.save import fresh_path

LINKTYPE_IEEE802_11 = 105
PCAP_MAGIC = 0xA1B2C3D4
PCAP_VERSION = (2, 4)
SNAPLEN = 65535


def write_pcap(path: Path, records: Iterable[tuple[bytes, float]]) -> int:
    """Write *records*, ``(raw 802.11 frame, capture timestamp)`` pairs, to a
    pcap at *path*. The timestamp is epoch seconds (float).

    Per-frame timing is preserved so the file is forensically accurate AND
    re-extractable by ``hcxpcapngtool``, which pairs EAPOL frames by their
    timestamps (an EAPOL timeout window). Identical stamps would make it
    mis-pair. A timestamp <= 0 (unset) falls back to the current wall-clock
    time so the frame still lands with a sane epoch.

    Returns the number of frames written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    fallback = time.time()
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
        for frame, ts in records:
            if not frame:
                continue
            t = ts if ts and ts > 0 else fallback
            ts_sec = int(t)
            ts_usec = int((t - ts_sec) * 1_000_000)
            length = len(frame)
            f.write(struct.pack("<IIII", ts_sec, ts_usec, length, length))
            f.write(frame)
            count += 1
    return count


def read_pcap(path: Path) -> Iterator[tuple[bytes, int]]:
    """``(frame, epoch seconds)`` of each record in a little-endian pcap written by ``write_pcap``;
    nothing for any other format or an unreadable file."""
    try:
        data = path.read_bytes()
    except OSError:
        return
    if len(data) < 24 or struct.unpack_from("<I", data)[0] != PCAP_MAGIC:
        return
    offset = 24
    while offset + 16 <= len(data):
        ts_sec, _, length, _ = struct.unpack_from("<IIII", data, offset)
        offset += 16
        yield data[offset:offset + length], ts_sec
        offset += length


class HandshakePcaps:
    """One pcap per handshake: the beacon and every frame of its 4-way. Rewrites the pcap when
    the handshake gains frames, for the handshakes written this session."""

    def __init__(self) -> None:
        # (bssid, client_mac, anonce) -> (pcap_path, num_messages)
        self._written: dict[tuple[str, str, bytes], tuple[Path, int]] = {}

    def save(self, ap: AccessPoint, client_mac: str) -> None:
        hs = ap.handshakes.get(client_mac)
        if hs is None:
            return
        for anonce, pair in hs.valid_pairs_by_instance().items():
            messages = handshake_messages(hs, pair)
            key = (ap.bssid, client_mac, anonce)
            if key in self._written:
                path, frame_count = self._written[key]
                if len(messages) <= frame_count:
                    continue
            else:
                path = fresh_path(Path(Config.captures_dir), ap.ssid, ap.bssid, "_handshake.pcap")
            records = [(hs.beacon_frame, messages[0].timestamp)]
            records += [(message.raw, message.timestamp) for message in messages]
            write_pcap(path, records)
            self._written[key] = (path, len(messages))
