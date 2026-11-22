"""libpcap writer: saves LINKTYPE_IEEE802_11 frames verbatim. Callers deliver
FCS-less MPDU bodies (every chip driver strips at RX ingress), so the writer
no longer second-guesses the frame tail."""

from wifit3.models import AccessPoint, Handshake, HandshakeMessage
from wifit3.persist.pcap import HandshakePcaps, read_pcap, write_pcap


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
    pcaps.save(ap, _CLIENT)
    [pcap] = tmp_path.glob("*_handshake.pcap")
    assert _frames(pcap)[-1] == m3.raw


def test_handshake_pcaps_skips_a_handshake_with_no_new_frames(tmp_path):
    pcaps = HandshakePcaps()
    ap = _ap(_message(1, 1, _ANONCE, 100.0), _message(2, 1, _SNONCE, 100.01))
    pcaps.save(ap, _CLIENT)
    [pcap] = tmp_path.glob("*_handshake.pcap")
    pcap.unlink()
    pcaps.save(ap, _CLIENT)
    assert list(tmp_path.glob("*_handshake.pcap")) == []


def test_handshake_pcaps_one_pcap_per_handshake(tmp_path):
    ap = _ap(_message(1, 1, _ANONCE, 100.0), _message(2, 1, _SNONCE, 100.01),
             _message(1, 1, b"\xbb" * 32, 120.0), _message(2, 1, b"\x33" * 32, 120.01))
    HandshakePcaps().save(ap, _CLIENT)
    assert len(list(tmp_path.glob("*_handshake.pcap"))) == 2
