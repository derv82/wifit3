"""Minimal libpcap writer for raw 802.11 frames.

Produces a standard libpcap-format file (NOT pcapng) with linktype
``LINKTYPE_IEEE802_11`` (105). Suitable for handing to ``hcxpcapngtool``
(which accepts both pcap and pcapng) → ``hashcat -m 22000``.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

from wifit3.crack.handshake import CrackablePair, handshake_messages, hc22000_line, messages_label
from wifit3.crack.hc22000_format import Hc22000Line, parse_hc22000
from wifit3.crack.wpa_psk import psk_matches
from wifit3.dot11.packet import BeaconPacket, EapolPacket
from wifit3.dot11.parser import WlanFrameParser
from wifit3.models import AccessPoint, Handshake, HandshakeMessage
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


def read_pcap(path: Path) -> Iterator[tuple[bytes, float]]:
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
        ts_sec, ts_usec, length, _ = struct.unpack_from("<IIII", data, offset)
        offset += 16
        yield data[offset:offset + length], ts_sec + ts_usec / 1_000_000
        offset += length


@dataclass(frozen=True)
class FourWayHandshake:
    """One 4-way of one client (one ANonce): its frames in arrival order and the AP's beacon."""
    bssid: str
    client_mac: str
    ssid: str                       # "" when unknown (hidden AP, beacon without an SSID)
    beacon: bytes
    messages: tuple[HandshakeMessage, ...]
    authorized: bool                # the crackable pair is M2+M3 / M3+M4 / M1+M4: the AP accepted the M2
    hashline: Optional[Hc22000Line] # the pair's WPA*02/04 line; None without an SSID

    @classmethod
    def from_pair(cls, hs: Handshake, pair: CrackablePair, ssid: str) -> FourWayHandshake:
        first, second = sorted((pair.anonce_frame.msg_num, pair.mic_frame.msg_num))
        hashline = parse_hc22000(hc22000_line(ssid, hs, pair)) if ssid else None
        return cls(hs.bssid, hs.client_mac, ssid, hs.beacon_frame or b"",
                   tuple(handshake_messages(hs, pair)), second > 2, hashline)

    @property
    def first_seen(self) -> float:
        return self.messages[0].timestamp

    @property
    def messages_label(self) -> str:
        return messages_label(self.messages)

    def matches_psk(self, psk: str) -> bool:
        """True if the passphrase (or 64-hex raw PMK) cracks this 4-way."""
        return self.hashline is not None and psk_matches(psk, self.hashline)

    def write(self, path: Path) -> int:
        """The beacon, then every frame, as a pcap at ``path``; returns the frame count."""
        records = [(self.beacon, self.first_seen)]
        records += [(message.raw, message.timestamp) for message in self.messages]
        return write_pcap(path, records)


class HandshakePcaps:
    """One pcap per handshake: the beacon and every frame of its 4-way. Rewrites the pcap when
    the handshake gains frames, for the handshakes written this session."""

    def __init__(self) -> None:
        # (bssid, client_mac, anonce) -> (pcap_path, num_messages)
        self._written: dict[tuple[str, str, bytes], tuple[Path, int]] = {}

    def save(self, ap: AccessPoint, client_mac: str) -> list[Path]:
        """Writes or rewrites the client's handshakes that gained frames; returns the paths written."""
        hs = ap.handshakes.get(client_mac)
        if hs is None:
            return []
        written: list[Path] = []
        for anonce, pair in hs.valid_pairs_by_instance().items():
            handshake = FourWayHandshake.from_pair(hs, pair, ap.ssid or "")
            key = (ap.bssid, client_mac, anonce)
            if key in self._written:
                path, frame_count = self._written[key]
                if len(handshake.messages) <= frame_count:
                    continue
            else:
                path = fresh_path(Path(Config.captures_dir), ap.ssid, ap.bssid, "_handshake.pcap")
            handshake.write(path)
            self._written[key] = (path, len(handshake.messages))
            written.append(path)
        return written


def read_handshakes(bssid: str, ssid: Optional[str], paths: Iterable[Path]) -> list[FourWayHandshake]:
    """Every 4-way of the AP in its pcaps, newest first. A frame in several pcaps is read once:
    a pcap saved before one-pcap-per-handshake holds every earlier frame of its client."""
    timestamps: dict[bytes, float] = {}
    for path in paths:
        for frame, timestamp in read_pcap(path):
            timestamps.setdefault(frame, timestamp)
    beacon: Optional[BeaconPacket] = None
    by_client: dict[str, Handshake] = {}
    for frame, timestamp in sorted(timestamps.items(), key=lambda item: item[1]):
        packet = WlanFrameParser.parse_80211_frame(frame, 0)
        if packet is None or packet.bssid != bssid:
            continue
        if isinstance(packet, BeaconPacket):
            beacon = packet
        elif isinstance(packet, EapolPacket) and packet.client_mac and packet.replay_counter:
            if packet.client_mac not in by_client:
                by_client[packet.client_mac] = Handshake(bssid=bssid, client_mac=packet.client_mac)
            by_client[packet.client_mac].messages.append(HandshakeMessage.from_packet(packet, packet.akm, timestamp))
    if beacon is None:
        return []
    ssid = beacon.ssid or ssid or ""
    handshakes: list[FourWayHandshake] = []
    for hs in by_client.values():
        hs.beacon_frame = beacon.raw
        hs.akm_offered = list(beacon.akm_suites)
        hs.vendor_akm_offered = beacon.vendor_akm_offered
        hs.rsn_ie_seen = beacon.rsn_ie_raw is not None
        for pair in hs.valid_pairs_by_instance().values():
            handshakes.append(FourWayHandshake.from_pair(hs, pair, ssid))
    handshakes.sort(key=lambda handshake: handshake.first_seen, reverse=True)
    return handshakes
