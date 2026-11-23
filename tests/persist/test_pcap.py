"""libpcap writer: saves LINKTYPE_IEEE802_11 frames verbatim. Callers deliver
FCS-less MPDU bodies (every chip driver strips at RX ingress), so the writer
no longer second-guesses the frame tail."""

from wifit3.dot11.eapol import LLC_SNAP_EAPOL, data_header, eapol_key, set_mic
from wifit3.dot11.mac import BROADCAST_MAC, mac_header
from wifit3.dot11.wpa import KCK_LEN, derive_pmk, derive_ptk, eapol_mic
from wifit3.models import AccessPoint, Handshake, HandshakeMessage
from wifit3.persist.pcap import HandshakePcaps, read_handshakes, read_pcap, write_pcap


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


def _m1_m2(index: int, timestamp: float) -> list[tuple[bytes, float]]:
    """The M1 and M2 of the ``index``th association, whose MIC ``_PSK`` makes."""
    anonce, snonce = b"\xa0" + index.to_bytes(31, "big"), b"\x5b" + index.to_bytes(31, "big")
    m1 = eapol_key(key_info=0x008a, key_len=16, replay=index, nonce=anonce)
    m2 = eapol_key(key_info=0x010a, key_len=0, replay=index, nonce=snonce, key_data=_RSN_IE_PSK)
    m2 = set_mic(m2, eapol_mic(derive_ptk(_PMK, _AA, _SPA, anonce, snonce, KCK_LEN), m2))
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
