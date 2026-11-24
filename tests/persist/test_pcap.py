"""libpcap writer: saves LINKTYPE_IEEE802_11 frames verbatim. Callers deliver
FCS-less MPDU bodies (every chip driver strips at RX ingress), so the writer
no longer second-guesses the frame tail."""

import struct

import pytest

from wifit3.crack.hc22000_format import pmkid_hashline, read_hc22000
from wifit3.crack.wpa_psk import psk_matches
from wifit3.dot11.eapol import LLC_SNAP_EAPOL, data_header, eapol_key, set_mic
from wifit3.dot11.mac import BROADCAST_MAC, mac_header
from wifit3.dot11.wpa import KCK_LEN, derive_pmk, derive_ptk, eapol_mic
from wifit3.models import AccessPoint, Handshake, HandshakeMessage
from wifit3.persist.pcap import (
    LINKTYPE_IEEE802_11,
    LINKTYPE_IEEE802_11_RADIOTAP,
    LINKTYPE_PRISM_HEADER,
    PCAP_LINKTYPE_FCS_PRESENT,
    PCAP_MAGIC_NANOSECONDS,
    PCAPNG_ENHANCED_PACKET_BLOCK,
    PCAPNG_IF_FCSLEN,
    PCAPNG_IF_TSOFFSET,
    PCAPNG_IF_TSRESOL,
    PCAPNG_INTERFACE_DESCRIPTION_BLOCK,
    PCAPNG_SECTION_HEADER_BLOCK,
    RADIOTAP_FLAGS_FCS,
    RADIOTAP_PRESENT_EXT,
    RADIOTAP_PRESENT_FLAGS,
    RADIOTAP_PRESENT_TSFT,
    FourWayHandshake,
    HandshakePcaps,
    read_access_points,
    read_handshakes,
    read_pcap,
    write_pcap,
)
from wifit3.persist.save import save_handshake


def _first_packet(path) -> bytes:
    """Extract the first packet's payload from a libpcap file (24-byte global
    header + 16-byte record header, all little-endian)."""
    raw = path.read_bytes()
    caplen = int.from_bytes(raw[24 + 8: 24 + 12], "little")
    return raw[40: 40 + caplen]


def _beacon_body() -> bytes:
    # Minimal beacon-ish MPDU: 24-byte hdr + 12 fixed params + SSID IE.
    return (
        b"\x80\x00" + b"\x00" * 22          # FC + dur + addr1/2/3 + seq (24 B)
        + b"\x00" * 12                       # timestamp/interval/caps
        + b"\x00\x04test"                    # SSID IE: tag 0, len 4
    )


def _record_header(path, index: int) -> tuple[int, int, int]:
    """(ts_sec, ts_usec, caplen) of the Nth packet record (little-endian).
    24-byte global header, then 16-byte record headers + payloads."""
    raw = path.read_bytes()
    off = 24
    for _ in range(index):
        caplen = int.from_bytes(raw[off + 8: off + 12], "little")
        off += 16 + caplen
    sec = int.from_bytes(raw[off: off + 4], "little")
    usec = int.from_bytes(raw[off + 4: off + 8], "little")
    caplen = int.from_bytes(raw[off + 8: off + 12], "little")
    return sec, usec, caplen


def test_write_pcap_writes_frame_verbatim(tmp_path):
    body = _beacon_body()
    path = tmp_path / "a.pcap"
    assert write_pcap(path, [(body, 0.0)]) == 1
    assert _first_packet(path) == body


def test_write_pcap_preserves_per_frame_timestamps(tmp_path):
    # Each frame keeps its own capture time (epoch seconds, µs resolution),
    # required so a round-tripped pcap re-extracts correctly in hcxpcapngtool.
    path = tmp_path / "b.pcap"
    recs = [(_beacon_body(), 1000.5), (_beacon_body(), 1002.25)]
    assert write_pcap(path, recs) == 2
    assert _record_header(path, 0)[:2] == (1000, 500000)
    assert _record_header(path, 1)[:2] == (1002, 250000)


def test_read_pcap_keeps_microseconds(tmp_path):
    path = tmp_path / "c.pcap"
    write_pcap(path, [(_beacon_body(), 100.25)])
    assert list(read_pcap(path)) == [(_beacon_body(), 100.25)]


def test_read_pcap_reads_big_endian_nanosecond_pcap(tmp_path):
    path = tmp_path / "be.pcap"
    body = _beacon_body()
    path.write_bytes(struct.pack(">IHHiIII", PCAP_MAGIC_NANOSECONDS, 2, 4, 0, 0, 65535, LINKTYPE_IEEE802_11)
                     + struct.pack(">IIII", 100, 250_000_000, len(body), len(body)) + body)
    assert list(read_pcap(path)) == [(body, 100.25)]


def test_read_pcap_skips_a_truncated_last_record(tmp_path):
    path = tmp_path / "a.pcap"
    write_pcap(path, [(_beacon_body(), 100.0), (_beacon_body(), 101.0)])
    path.write_bytes(path.read_bytes()[:-1])
    assert list(read_pcap(path)) == [(_beacon_body(), 100.0)]


def _pcapng_block(order: str, block_type: int, body: bytes) -> bytes:
    body += bytes(-len(body) % 4)
    length = 12 + len(body)
    return struct.pack(order + "II", block_type, length) + body + struct.pack(order + "I", length)


def _pcapng_section(order: str) -> bytes:
    return _pcapng_block(order, PCAPNG_SECTION_HEADER_BLOCK, struct.pack(order + "IHHq", 0x1A2B3C4D, 1, 0, -1))


def _pcapng_option(order: str, code: int, value: bytes) -> bytes:
    return struct.pack(order + "HH", code, len(value)) + value + bytes(-len(value) % 4)


def _pcapng_interface(order: str, linktype: int, *options: bytes) -> bytes:
    return _pcapng_block(order, PCAPNG_INTERFACE_DESCRIPTION_BLOCK,
                         struct.pack(order + "HHI", linktype, 0, 65535) + b"".join(options) + bytes(4))


def _pcapng_packet(order: str, interface_id: int, timestamp: int, packet: bytes, original_length: int = 0) -> bytes:
    header = struct.pack(order + "IIIII", interface_id, timestamp >> 32, timestamp & 0xFFFFFFFF,
                         len(packet), original_length or len(packet))
    return _pcapng_block(order, PCAPNG_ENHANCED_PACKET_BLOCK, header + packet)


@pytest.mark.parametrize("order", ["<", ">"])
def test_read_pcap_reads_pcapng_sections_and_interfaces(tmp_path, order):
    body = _beacon_body()
    path = tmp_path / "a.pcapng"
    path.write_bytes(
        _pcapng_section(order)
        + _pcapng_interface(order, LINKTYPE_IEEE802_11)
        + _pcapng_interface(order, LINKTYPE_IEEE802_11, _pcapng_option(order, PCAPNG_IF_TSRESOL, bytes([9])))
        + _pcapng_block(order, 4, bytes(8))
        + _pcapng_packet(order, 0, 100_250_000, body)
        + _pcapng_packet(order, 1, 200_500_000_000, body)
        + _pcapng_section(order)
        + _pcapng_interface(order, LINKTYPE_IEEE802_11, _pcapng_option(order, PCAPNG_IF_TSRESOL, bytes([0x81])))
        + _pcapng_packet(order, 0, 601, body))
    assert list(read_pcap(path)) == [(body, 100.25), (body, 200.5), (body, 300.5)]


def test_read_pcap_skips_a_truncated_pcapng_block(tmp_path):
    path = tmp_path / "a.pcapng"
    data = _pcapng_section("<") + _pcapng_interface("<", LINKTYPE_IEEE802_11) + _pcapng_packet("<", 0, 0, _beacon_body())
    path.write_bytes(data[:-1])
    assert list(read_pcap(path)) == []


def test_read_pcap_strips_radiotap_header_and_fcs(tmp_path):
    present = RADIOTAP_PRESENT_EXT | RADIOTAP_PRESENT_FLAGS | RADIOTAP_PRESENT_TSFT
    radiotap = struct.pack("<BBHII", 0, 0, 25, present, 0) + bytes(4) + bytes(8) + bytes([RADIOTAP_FLAGS_FCS])
    path = tmp_path / "a.pcapng"
    path.write_bytes(_pcapng_section("<") + _pcapng_interface("<", LINKTYPE_IEEE802_11_RADIOTAP)
                     + _pcapng_packet("<", 0, 0, radiotap + _beacon_body() + b"FCS!"))
    assert _frames(path) == [_beacon_body()]


def test_read_pcap_keeps_the_frame_tail_without_radiotap_flags(tmp_path):
    radiotap = struct.pack("<BBHI", 0, 0, 8, 0)
    path = tmp_path / "a.pcapng"
    path.write_bytes(_pcapng_section("<") + _pcapng_interface("<", LINKTYPE_IEEE802_11_RADIOTAP)
                     + _pcapng_packet("<", 0, 0, radiotap + _beacon_body()))
    assert _frames(path) == [_beacon_body()]


def test_read_pcap_strips_prism_header(tmp_path):
    prism = struct.pack("<II", 0x41, 144) + bytes(136)
    path = tmp_path / "a.pcapng"
    path.write_bytes(_pcapng_section("<") + _pcapng_interface("<", LINKTYPE_PRISM_HEADER)
                     + _pcapng_packet("<", 0, 0, prism + _beacon_body()))
    assert _frames(path) == [_beacon_body()]


def _pcap(linktype_field: int, *records: tuple[bytes, int]) -> bytes:
    data = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype_field)
    for packet, original_length in records:
        data += struct.pack("<IIII", 100, 0, len(packet), original_length) + packet
    return data


def test_read_pcap_skips_a_record_cut_by_the_snaplen(tmp_path):
    path = tmp_path / "a.pcap"
    path.write_bytes(_pcap(LINKTYPE_IEEE802_11, (_beacon_body(), len(_beacon_body()) + 4), (b"NEXT", 4)))
    assert _frames(path) == [b"NEXT"]


def test_read_pcap_skips_a_pcapng_packet_cut_by_the_snaplen(tmp_path):
    path = tmp_path / "a.pcapng"
    path.write_bytes(_pcapng_section("<") + _pcapng_interface("<", LINKTYPE_IEEE802_11)
                     + _pcapng_packet("<", 0, 0, _beacon_body(), original_length=len(_beacon_body()) + 4))
    assert _frames(path) == []


@pytest.mark.parametrize("header", [
    struct.pack(">II", 0x44, 144) + bytes(136),
    struct.pack(">II", 0x80211001, 64) + bytes(56),
])
def test_read_pcap_strips_big_endian_prism_and_avs_headers(tmp_path, header):
    path = tmp_path / "a.pcapng"
    path.write_bytes(_pcapng_section("<") + _pcapng_interface("<", LINKTYPE_PRISM_HEADER)
                     + _pcapng_packet("<", 0, 0, header + _beacon_body()))
    assert _frames(path) == [_beacon_body()]


@pytest.mark.parametrize("linktype, header", [
    (LINKTYPE_IEEE802_11_RADIOTAP, struct.pack("<BBHI", 0, 0, 4, 0)),
    (LINKTYPE_PRISM_HEADER, struct.pack("<II", 0x44, 8)),
    (LINKTYPE_PRISM_HEADER, struct.pack(">II", 0x80211001, 8)),
])
def test_read_pcap_yields_nothing_for_a_link_header_below_its_minimum_length(tmp_path, linktype, header):
    path = tmp_path / "a.pcapng"
    path.write_bytes(_pcapng_section("<") + _pcapng_interface("<", linktype)
                     + _pcapng_packet("<", 0, 0, header + _beacon_body()))
    assert _frames(path) == []


def test_read_pcap_adds_the_pcapng_timestamp_offset(tmp_path):
    tsoffset = _pcapng_option("<", PCAPNG_IF_TSOFFSET, struct.pack("<q", 1000))
    path = tmp_path / "a.pcapng"
    path.write_bytes(_pcapng_section("<") + _pcapng_interface("<", LINKTYPE_IEEE802_11, tsoffset)
                     + _pcapng_packet("<", 0, 250_000, _beacon_body()))
    assert list(read_pcap(path)) == [(_beacon_body(), 1000.25)]


def test_read_pcap_strips_the_fcs_of_a_classic_pcap_link_type(tmp_path):
    fcs_words = 2
    path = tmp_path / "a.pcap"
    path.write_bytes(_pcap((fcs_words << 28) | PCAP_LINKTYPE_FCS_PRESENT | LINKTYPE_IEEE802_11,
                           (_beacon_body() + b"FCS!", len(_beacon_body()) + 4)))
    assert _frames(path) == [_beacon_body()]


def test_read_pcap_strips_the_fcs_of_a_pcapng_interface(tmp_path):
    fcslen = _pcapng_option("<", PCAPNG_IF_FCSLEN, bytes([32]))
    path = tmp_path / "a.pcapng"
    path.write_bytes(_pcapng_section("<") + _pcapng_interface("<", LINKTYPE_IEEE802_11, fcslen)
                     + _pcapng_packet("<", 0, 0, _beacon_body() + b"FCS!"))
    assert _frames(path) == [_beacon_body()]


def test_read_pcap_yields_nothing_for_other_link_types(tmp_path):
    path = tmp_path / "a.pcapng"
    path.write_bytes(_pcapng_section("<") + _pcapng_interface("<", 1) + _pcapng_packet("<", 0, 0, _beacon_body()))
    assert list(read_pcap(path)) == []


_CLIENT = "11:22:33:44:55:66"
_ANONCE = b"\xaa" * 32
_SNONCE = b"\x22" * 32


def _message(msg_num: int, replay: int, nonce: bytes, ts: float) -> HandshakeMessage:
    return HandshakeMessage(
        raw=bytes([msg_num, replay]) + nonce[:1], msg_num=msg_num,
        replay_hex=replay.to_bytes(8, "big").hex(), nonce=nonce, mic=b"\x11" * 16,
        eapol_payload=bytes(120), timestamp=ts,
    )


def _ap(*messages: HandshakeMessage) -> AccessPoint:
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
    hs = Handshake(bssid=ap.bssid, client_mac=_CLIENT, beacon_frame=b"BEACON")
    hs.messages.extend(messages)
    ap.handshakes[_CLIENT] = hs
    return ap


def _frames(path) -> list[bytes]:
    return [frame for frame, _ in read_pcap(path)]


def test_handshake_pcaps_writes_the_beacon_and_the_4way(tmp_path):
    m1 = _message(1, 1, _ANONCE, 100.0)
    m2 = _message(2, 1, _SNONCE, 100.01)
    HandshakePcaps().save(_ap(m1, m2), _CLIENT)
    [pcap] = tmp_path.glob("*_handshake.pcap")
    assert _frames(pcap) == [b"BEACON", m1.raw, m2.raw]


def test_handshake_pcaps_rewrites_the_same_pcap_when_the_handshake_gains_frames(tmp_path):
    pcaps = HandshakePcaps()
    ap = _ap(_message(1, 1, _ANONCE, 100.0), _message(2, 1, _SNONCE, 100.01))
    pcaps.save(ap, _CLIENT)
    m3 = _message(3, 2, _ANONCE, 100.02)
    ap.handshakes[_CLIENT].messages.append(m3)
    written = pcaps.save(ap, _CLIENT)
    [pcap] = tmp_path.glob("*_handshake.pcap")
    assert written == [pcap]
    assert _frames(pcap)[-1] == m3.raw


def test_handshake_pcaps_skips_a_handshake_with_no_new_frames(tmp_path):
    pcaps = HandshakePcaps()
    ap = _ap(_message(1, 1, _ANONCE, 100.0), _message(2, 1, _SNONCE, 100.01))
    pcaps.save(ap, _CLIENT)
    [pcap] = tmp_path.glob("*_handshake.pcap")
    pcap.unlink()
    assert pcaps.save(ap, _CLIENT) == []
    assert list(tmp_path.glob("*_handshake.pcap")) == []


def test_handshake_pcaps_one_pcap_per_handshake(tmp_path):
    ap = _ap(_message(1, 1, _ANONCE, 100.0), _message(2, 1, _SNONCE, 100.01),
             _message(1, 1, b"\xbb" * 32, 120.0), _message(2, 1, b"\x33" * 32, 120.01))
    HandshakePcaps().save(ap, _CLIENT)
    assert len(list(tmp_path.glob("*_handshake.pcap"))) == 2


_BSSID = "02:00:00:00:00:01"
_AA = bytes.fromhex("020000000001")
_SPA = bytes.fromhex("020000000002")
_SSID = "HomeNet"
_PSK = "correct horse"
_PMK = derive_pmk(_PSK, _SSID.encode())
_RSN_IE_PSK = bytes.fromhex("30140100000fac040100000fac040100000fac020000")


def _beacon() -> bytes:
    fixed = bytes(8) + bytes.fromhex("64001104")
    return mac_header(b"\x80\x00", BROADCAST_MAC, _AA, _AA) + fixed + b"\x00\x07HomeNet\x01\x01\x82" + _RSN_IE_PSK


def _m1_m2(index: int, timestamp: float, pmk: bytes = _PMK) -> list[tuple[bytes, float]]:
    """The M1 and M2 of the ``index``th association, whose MIC ``pmk`` makes."""
    anonce, snonce = b"\xa0" + index.to_bytes(31, "big"), b"\x5b" + index.to_bytes(31, "big")
    m1 = eapol_key(key_info=0x008a, key_len=16, replay=index, nonce=anonce)
    m2 = eapol_key(key_info=0x010a, key_len=0, replay=index, nonce=snonce, key_data=_RSN_IE_PSK)
    m2 = set_mic(m2, eapol_mic(derive_ptk(pmk, _AA, _SPA, anonce, snonce, KCK_LEN), m2))
    return [(data_header(to_ds=False, bssid=_AA, client=_SPA) + LLC_SNAP_EAPOL + m1, timestamp),
            (data_header(to_ds=True, bssid=_AA, client=_SPA) + LLC_SNAP_EAPOL + m2, timestamp + 0.005)]


def test_read_handshakes_reads_each_4way_once_from_cumulative_pcaps(tmp_path):
    first, second = _m1_m2(1, 1000.0), _m1_m2(2, 1060.0)
    write_pcap(tmp_path / "old.pcap", [(_beacon(), 1000.0), *first])
    write_pcap(tmp_path / "new.pcap", [(_beacon(), 1000.0), *first, *second])
    handshakes = read_handshakes(_BSSID, None, [tmp_path / "old.pcap", tmp_path / "new.pcap"])
    assert [handshake.first_seen for handshake in handshakes] == [1060.0, 1000.0]
    assert [handshake.messages_label for handshake in handshakes] == ["M1+M2", "M1+M2"]


def test_read_handshakes_pairs_frames_out_of_order_across_files_by_time(tmp_path):
    m1, m2 = _m1_m2(1, 1000.0)
    write_pcap(tmp_path / "a.pcap", [(_beacon(), 999.0), m2])
    write_pcap(tmp_path / "b.pcap", [(_beacon(), 999.0), m1])
    [handshake] = read_handshakes(_BSSID, None, [tmp_path / "a.pcap", tmp_path / "b.pcap"])
    assert [message.raw for message in handshake.messages] == [m1[0], m2[0]]


def test_read_handshakes_keeps_every_4way_of_300_eapol_frames(tmp_path):
    records = [(_beacon(), 999.0)]
    for index in range(1, 151):
        records += _m1_m2(index, 1000.0 + 10 * index)
    write_pcap(tmp_path / "a.pcap", records)
    assert len(read_handshakes(_BSSID, None, [tmp_path / "a.pcap"])) == 150


def test_read_handshakes_without_a_beacon_is_empty(tmp_path):
    write_pcap(tmp_path / "a.pcap", _m1_m2(1, 1000.0))
    assert read_handshakes(_BSSID, _SSID, [tmp_path / "a.pcap"]) == []


def test_four_way_handshake_write_round_trips_through_read_pcap(tmp_path):
    write_pcap(tmp_path / "a.pcap", [(_beacon(), 999.0), *_m1_m2(1, 1000.0)])
    [handshake] = read_handshakes(_BSSID, None, [tmp_path / "a.pcap"])
    assert handshake.write(tmp_path / "b.pcap") == 3
    assert _frames(tmp_path / "b.pcap") == [handshake.beacon, *(message.raw for message in handshake.messages)]


def test_four_way_handshake_matches_psk(tmp_path):
    write_pcap(tmp_path / "a.pcap", [(_beacon(), 999.0), *_m1_m2(1, 1000.0)])
    [handshake] = read_handshakes(_BSSID, None, [tmp_path / "a.pcap"])
    assert handshake.ssid == _SSID
    assert handshake.matches_psk(_PSK)
    assert not handshake.matches_psk("wrong horse")


_CLIENT_MAC = "02:00:00:00:00:02"


def _hidden_beacon() -> bytes:
    fixed = bytes(8) + bytes.fromhex("64001104")
    return mac_header(b"\x80\x00", BROADCAST_MAC, _AA, _AA) + fixed + b"\x00\x00\x01\x01\x82" + _RSN_IE_PSK


def _probe_response() -> bytes:
    fixed = bytes(8) + bytes.fromhex("64001104")
    return mac_header(b"\x50\x00", _SPA, _AA, _AA) + fixed + b"\x00\x07HomeNet\x01\x01\x82" + _RSN_IE_PSK


def _assoc_request() -> bytes:
    return mac_header(b"\x00\x00", _AA, _SPA, _AA) + bytes.fromhex("11040a00") + b"\x00\x07HomeNet" + _RSN_IE_PSK


def test_read_access_points_stamps_messages_and_last_seen_with_frame_times(tmp_path):
    write_pcap(tmp_path / "a.pcap", [(_beacon(), 999.0), *_m1_m2(1, 1000.0), (_beacon(), 1001.0)])
    [ap] = read_access_points(tmp_path / "a.pcap")
    assert (ap.bssid, ap.ssid, ap.last_seen) == (_BSSID, _SSID, 1001.0)
    assert [message.timestamp for message in ap.handshakes[_CLIENT_MAC].messages] == pytest.approx([1000.0, 1000.005], abs=1e-6)


def test_read_access_points_uses_a_probe_response_as_the_beacon_of_an_ap_without_beacons(tmp_path):
    write_pcap(tmp_path / "a.pcap", [(_probe_response(), 999.0), *_m1_m2(1, 1000.0)])
    [ap] = read_access_points(tmp_path / "a.pcap")
    hs = ap.handshakes[_CLIENT_MAC]
    assert hs.beacon_frame == _probe_response()
    [pair] = hs.valid_pairs_by_instance().values()
    assert FourWayHandshake.from_pair(hs, pair, ap.ssid, ap.ssid_bytes).matches_psk(_PSK)


@pytest.mark.parametrize("naming_frame", [_probe_response(), _assoc_request()])
def test_read_access_points_decloaks_a_hidden_ap(tmp_path, naming_frame):
    write_pcap(tmp_path / "a.pcap", [(_hidden_beacon(), 999.0), (naming_frame, 999.5), *_m1_m2(1, 1000.0)])
    [ap] = read_access_points(tmp_path / "a.pcap")
    assert ap.ssid == _SSID


def test_read_access_points_leaves_a_hidden_ap_without_an_ssid(tmp_path):
    write_pcap(tmp_path / "a.pcap", [(_hidden_beacon(), 999.0), *_m1_m2(1, 1000.0)])
    [ap] = read_access_points(tmp_path / "a.pcap")
    assert ap.ssid is None
    assert ap.handshakes[_CLIENT_MAC].valid_pairs_by_instance()


def test_read_access_points_reads_a_pmkid_from_a_file_without_m2(tmp_path):
    pmkid = bytes(range(16))
    m1 = eapol_key(key_info=0x008a, key_len=16, replay=1, nonce=b"\xa0" * 32,
                   key_data=bytes.fromhex("dd14000fac04") + pmkid)
    m1_frame = data_header(to_ds=False, bssid=_AA, client=_SPA) + LLC_SNAP_EAPOL + m1
    write_pcap(tmp_path / "a.pcap", [(_beacon(), 999.0), (m1_frame, 1000.0)])
    [ap] = read_access_points(tmp_path / "a.pcap")
    hs = ap.handshakes[_CLIENT_MAC]
    assert hs.pmkid == pmkid
    assert pmkid_hashline(ap.ssid_bytes, hs) is not None


# (SSID element, ap.ssid, the .hc22000 filename): ap.ssid and the filename are as on master.
_CAFE_SSIDS = [
    pytest.param(b"Caf\xc3\xa9", "Café", "Caf__02-00-00-00-00-01.hc22000", id="utf-8"),
    pytest.param(b"Caf\xe9", "Caf", "Caf_02-00-00-00-00-01.hc22000", id="latin-1"),
]


def _beacon_with_ssid(ssid_bytes: bytes) -> bytes:
    fixed = bytes(8) + bytes.fromhex("64001104")
    return (mac_header(b"\x80\x00", BROADCAST_MAC, _AA, _AA) + fixed + bytes([0, len(ssid_bytes)]) + ssid_bytes
            + b"\x01\x01\x82" + _RSN_IE_PSK)


@pytest.mark.parametrize(("ssid_bytes", "ssid", "filename"), _CAFE_SSIDS)
def test_save_handshake_writes_the_ssid_bytes_as_the_essid(tmp_path, ssid_bytes, ssid, filename):
    pmk = derive_pmk(_PSK, ssid_bytes)
    write_pcap(tmp_path / "a.pcap", [(_beacon_with_ssid(ssid_bytes), 999.0), *_m1_m2(1, 1000.0, pmk)])
    [ap] = read_access_points(tmp_path / "a.pcap")
    result = save_handshake(ap, _CLIENT_MAC)
    assert ap.ssid == ssid
    assert result.path.name == filename
    [(_, line)] = read_hc22000(result.path)
    assert line.essid == ssid_bytes.hex()
    assert psk_matches(_PSK, line)
    assert line.ssid == ssid


@pytest.mark.parametrize(("ssid_bytes", "ssid", "filename"), _CAFE_SSIDS)
def test_read_handshakes_hashes_the_ssid_bytes(tmp_path, ssid_bytes, ssid, filename):
    pmk = derive_pmk(_PSK, ssid_bytes)
    write_pcap(tmp_path / "a.pcap", [(_beacon_with_ssid(ssid_bytes), 999.0), *_m1_m2(1, 1000.0, pmk)])
    [handshake] = read_handshakes(_BSSID, None, [tmp_path / "a.pcap"])
    assert handshake.ssid == ssid
    assert handshake.matches_psk(_PSK)
