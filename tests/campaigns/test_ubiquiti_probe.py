import struct

from wifit3.campaigns.ubiquiti_probe import (
    build_ubnt_discovery_frame,
    is_ubnt_plaintext_frame,
    is_ubnt_response,
)
from wifit3.dot11.mac import str_to_mac


_LLC_SNAP_IPV4 = b"\xaa\xaa\x03\x00\x00\x00\x08\x00"
_UBNT_PORT = 10001


def _fromds_frame(bssid: bytes, our_mac: bytes, body: bytes) -> bytes:
    return b"\x08\x02\x00\x00" + our_mac + bssid + b"\x00\x11\x22\x33\x44\x55" + b"\x00\x00" + body


def _tods_frame(bssid: bytes, our_mac: bytes, body: bytes) -> bytes:
    return b"\x08\x01\x00\x00" + bssid + our_mac + b"\xff" * 6 + b"\x00\x00" + body


def _udp_ipv4(src_port: int, dst_port: int) -> bytes:
    udp = struct.pack(">HHHH", src_port, dst_port, 8, 0)
    ip = b"\x45\x00\x00\x1c\x00\x00\x00\x00\x40\x11\x00\x00\xc0\xa8\x01\x01\xc0\xa8\x01\xff"
    return _LLC_SNAP_IPV4 + ip + udp


def test_build_ubnt_discovery_frame_is_tods_ipv4_udp_broadcast():
    bssid = str_to_mac("aa:bb:cc:dd:ee:ff")
    our_mac = str_to_mac("02:00:00:00:00:01")
    frame = build_ubnt_discovery_frame(bssid, our_mac)
    assert frame[:2] == b"\x08\x01"
    assert frame[4:10] == bssid
    assert frame[10:16] == our_mac
    assert frame[16:22] == b"\xff" * 6
    assert frame[24:32] == _LLC_SNAP_IPV4
    assert struct.unpack(">HH", frame[24 + 8 + 20:24 + 8 + 24]) == (_UBNT_PORT, _UBNT_PORT)


def test_ubnt_response_matches_fromds_udp_10001():
    bssid = str_to_mac("aa:bb:cc:dd:ee:ff")
    our_mac = str_to_mac("02:00:00:00:00:01")
    assert is_ubnt_response(_fromds_frame(bssid, our_mac, _udp_ipv4(_UBNT_PORT, 49152)))
    assert is_ubnt_response(_fromds_frame(bssid, our_mac, _udp_ipv4(49152, _UBNT_PORT)))
    assert not is_ubnt_response(_fromds_frame(bssid, our_mac, _udp_ipv4(53, 49152)))


def test_ubnt_plaintext_frame_can_match_tods_or_fromds():
    bssid = str_to_mac("aa:bb:cc:dd:ee:ff")
    our_mac = str_to_mac("02:00:00:00:00:01")
    fromds = _fromds_frame(bssid, our_mac, _udp_ipv4(_UBNT_PORT, 49152))
    tods = _tods_frame(bssid, our_mac, _udp_ipv4(49152, _UBNT_PORT))
    protected = bytes([fromds[0], fromds[1] | 0x40]) + fromds[2:]
    assert is_ubnt_plaintext_frame(fromds)
    assert is_ubnt_plaintext_frame(tods)
    assert not is_ubnt_plaintext_frame(protected)
