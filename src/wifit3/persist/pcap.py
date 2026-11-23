"""Reads 802.11 frames from pcap and pcapng files, writes raw 802.11 pcaps for ``hcxpcapngtool``,
and reads the 4-way handshakes in them."""

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
from wifit3.wlan.sink import WlanSink

LINKTYPE_IEEE802_11 = 105
LINKTYPE_PRISM_HEADER = 119
LINKTYPE_IEEE802_11_RADIOTAP = 127
PCAP_MAGIC = 0xA1B2C3D4
PCAP_MAGIC_NANOSECONDS = 0xA1B23C4D
PCAP_LINKTYPE_FCS_PRESENT = 0x04000000
PCAP_VERSION = (2, 4)
SNAPLEN = 65535
PCAPNG_SECTION_HEADER_BLOCK = 0x0A0D0D0A
PCAPNG_INTERFACE_DESCRIPTION_BLOCK = 1
PCAPNG_ENHANCED_PACKET_BLOCK = 6
PCAPNG_BYTE_ORDER_MAGIC = 0x1A2B3C4D
PCAPNG_IF_TSRESOL = 9
PCAPNG_IF_FCSLEN = 13
PCAPNG_IF_TSOFFSET = 14
PCAPNG_TSRESOL_POWER_OF_2 = 0x80
PCAPNG_TSRESOL_EXPONENT = 0x7F
PRISM_MSGCODES = (0x41, 0x44)
PRISM_HEADER_LENGTH = 144
AVS_HEADER_LENGTH_BY_COOKIE = {0x80211001: 64, 0x80211002: 80}
RADIOTAP_PRESENT_TSFT = 1 << 0
RADIOTAP_PRESENT_FLAGS = 1 << 1
RADIOTAP_PRESENT_EXT = 1 << 31
RADIOTAP_FLAGS_FCS = 0x10


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
    """``(802.11 frame, epoch seconds)`` of each complete record of a pcap or pcapng with raw 802.11,
    radiotap or Prism link type, link header stripped; nothing for other link types or an unreadable file."""
    try:
        data = path.read_bytes()
    except OSError:
        return
    if data[:4] == PCAPNG_SECTION_HEADER_BLOCK.to_bytes(4, "little"):
        packets = _pcapng_packets(data)
    else:
        packets = _pcap_packets(data)
    for linktype, fcs_length, packet, timestamp in packets:
        frame = _strip_link_header(linktype, fcs_length, packet)
        if frame is not None:
            yield frame, timestamp


def _pcap_packets(data: bytes) -> Iterator[tuple[int, int, bytes, float]]:
    if len(data) < 24:
        return
    magic = data[:4]
    if magic == PCAP_MAGIC.to_bytes(4, "little"):
        order, units_per_second = "<", 1_000_000
    elif magic == PCAP_MAGIC.to_bytes(4, "big"):
        order, units_per_second = ">", 1_000_000
    elif magic == PCAP_MAGIC_NANOSECONDS.to_bytes(4, "little"):
        order, units_per_second = "<", 1_000_000_000
    elif magic == PCAP_MAGIC_NANOSECONDS.to_bytes(4, "big"):
        order, units_per_second = ">", 1_000_000_000
    else:
        return
    linktype_field = struct.unpack_from(order + "I", data, 20)[0]
    fcs_length = 0
    if linktype_field & PCAP_LINKTYPE_FCS_PRESENT:
        fcs_words = linktype_field >> 28
        fcs_length = 2 * fcs_words
    offset = 24
    while offset + 16 <= len(data):
        ts_sec, ts_fraction, length, original_length = struct.unpack_from(order + "IIII", data, offset)
        offset += 16
        if offset + length > len(data):
            return
        if original_length <= length:
            yield (linktype_field & 0xFFFF, fcs_length, data[offset:offset + length],
                   ts_sec + ts_fraction / units_per_second)
        offset += length


def _pcapng_packets(data: bytes) -> Iterator[tuple[int, int, bytes, float]]:
    order = "<"
    interfaces: list[tuple[int, int, int, int]] = []
    offset = 0
    while offset + 12 <= len(data):
        block_type = struct.unpack_from(order + "I", data, offset)[0]
        if block_type == PCAPNG_SECTION_HEADER_BLOCK:
            byte_order_magic = data[offset + 8:offset + 12]
            if byte_order_magic == PCAPNG_BYTE_ORDER_MAGIC.to_bytes(4, "little"):
                order = "<"
            elif byte_order_magic == PCAPNG_BYTE_ORDER_MAGIC.to_bytes(4, "big"):
                order = ">"
            else:
                return
            interfaces = []
        block_length = struct.unpack_from(order + "I", data, offset + 4)[0]
        if block_length < 12 or offset + block_length > len(data):
            return
        body = data[offset + 8:offset + block_length - 4]
        if block_type == PCAPNG_INTERFACE_DESCRIPTION_BLOCK and len(body) >= 8:
            interfaces.append(_pcapng_interface(order, body))
        elif block_type == PCAPNG_ENHANCED_PACKET_BLOCK and len(body) >= 20:
            interface_id, ts_high, ts_low, length, original_length = struct.unpack_from(order + "IIIII", body)
            if interface_id < len(interfaces) and 20 + length <= len(body) and original_length <= length:
                linktype, fcs_length, units_per_second, ts_offset = interfaces[interface_id]
                timestamp = ts_offset + ((ts_high << 32) | ts_low) / units_per_second
                yield linktype, fcs_length, body[20:20 + length], timestamp
        offset += block_length


def _pcapng_interface(order: str, body: bytes) -> tuple[int, int, int, int]:
    """``(link type, FCS length, timestamp units per second, timestamp offset in seconds)`` of an IDB body."""
    fcs_length = 0
    units_per_second = 1_000_000
    ts_offset = 0
    option_offset = 8
    while option_offset + 4 < len(body):
        code, length = struct.unpack_from(order + "HH", body, option_offset)
        value_offset = option_offset + 4
        if value_offset + length > len(body):
            break
        if code == PCAPNG_IF_TSRESOL and length == 1:
            tsresol = body[value_offset]
            if tsresol & PCAPNG_TSRESOL_POWER_OF_2:
                units_per_second = 2 ** (tsresol & PCAPNG_TSRESOL_EXPONENT)
            else:
                units_per_second = 10 ** tsresol
        elif code == PCAPNG_IF_FCSLEN and length == 1:
            fcs_bits = body[value_offset]
            fcs_length = fcs_bits // 8
        elif code == PCAPNG_IF_TSOFFSET and length == 8:
            ts_offset = struct.unpack_from(order + "q", body, value_offset)[0]
        option_offset = value_offset + (length + 3) // 4 * 4
    return struct.unpack_from(order + "H", body)[0], fcs_length, units_per_second, ts_offset


def _strip_prism_header(packet: bytes) -> Optional[bytes]:
    if len(packet) < 8:
        return None
    avs_cookie = struct.unpack_from(">I", packet)[0]
    if avs_cookie in AVS_HEADER_LENGTH_BY_COOKIE:
        minimum_length = AVS_HEADER_LENGTH_BY_COOKIE[avs_cookie]
        header_length = struct.unpack_from(">I", packet, 4)[0]
    elif avs_cookie in PRISM_MSGCODES:
        minimum_length = PRISM_HEADER_LENGTH
        header_length = struct.unpack_from(">I", packet, 4)[0]
    elif struct.unpack_from("<I", packet)[0] in PRISM_MSGCODES:
        minimum_length = PRISM_HEADER_LENGTH
        header_length = struct.unpack_from("<I", packet, 4)[0]
    else:
        return packet
    if header_length < minimum_length or header_length > len(packet):
        return None
    return packet[header_length:]


def _strip_link_header(linktype: int, fcs_length: int, packet: bytes) -> Optional[bytes]:
    if linktype == LINKTYPE_IEEE802_11:
        if fcs_length > len(packet):
            return None
        return packet[:len(packet) - fcs_length]
    if linktype == LINKTYPE_PRISM_HEADER:
        return _strip_prism_header(packet)
    if linktype != LINKTYPE_IEEE802_11_RADIOTAP or len(packet) < 8:
        return None
    header_length, present = struct.unpack_from("<HI", packet, 2)
    if header_length < 8 or header_length > len(packet):
        return None
    frame = packet[header_length:]
    if present & RADIOTAP_PRESENT_FLAGS:
        fields_offset = 8
        present_word = present
        while present_word & RADIOTAP_PRESENT_EXT and fields_offset + 4 <= header_length:
            present_word = struct.unpack_from("<I", packet, fields_offset)[0]
            fields_offset += 4
        if present & RADIOTAP_PRESENT_TSFT:
            fields_offset = (fields_offset + 7) // 8 * 8 + 8
        if fields_offset < header_length and packet[fields_offset] & RADIOTAP_FLAGS_FCS:
            frame = frame[:-4]
    return frame


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


def read_access_points(path: Path) -> list[AccessPoint]:
    """Every AP in the capture with its handshakes and PMKIDs, stamped with the file's frame times.
    A handshake on an AP heard only through probe responses gets the first probe response as its beacon."""
    sink = WlanSink()
    probe_response_by_bssid: dict[str, bytes] = {}
    for frame, timestamp in read_pcap(path):
        packet = WlanFrameParser.parse_80211_frame(frame, 0)
        if packet is None:
            continue
        sink.update(packet, path.name, timestamp=timestamp)
        if isinstance(packet, BeaconPacket) and packet.type == "probe_resp":
            probe_response_by_bssid.setdefault(packet.bssid, packet.raw)
    for ap in sink.get_access_points():
        for hs in ap.handshakes.values():
            if not hs.beacon_frame and ap.bssid in probe_response_by_bssid:
                hs.beacon_frame = probe_response_by_bssid[ap.bssid]
    return sink.get_access_points()
