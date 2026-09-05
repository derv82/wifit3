import struct

from wifit3.campaigns.mikrotik_probe import (
    build_mikrotik_discovery_frames, is_mikrotik_plaintext_frame, is_mikrotik_response,
)
from wifit3.dot11.mac import str_to_mac


def _fromds_frame(bssid: bytes, our_mac: bytes, body: bytes) -> bytes:
    return b"\x08\x02\x00\x00" + our_mac + bssid + b"\x00\x11\x22\x33\x44\x55" + b"\x00\x00" + body


def _udp_ipv4(src_port: int, dst_port: int) -> bytes:
    udp = struct.pack(">HHHH", src_port, dst_port, 8, 0)
    ip = b"\x45\x00\x00\x1c\x00\x00\x00\x00\x40\x11\x00\x00\xc0\xa8\x58\x01\xc0\xa8\x58\xff"
    return b"\xaa\xaa\x03\x00\x00\x00\x08\x00" + ip + udp


def test_build_mikrotik_discovery_frames_are_tods_ipv4_udp():
    bssid = str_to_mac("aa:bb:cc:dd:ee:ff")
    our_mac = str_to_mac("02:00:00:00:00:01")
    frames = build_mikrotik_discovery_frames(bssid, our_mac)
    assert len(frames) == 2
    assert all(frame[:2] == b"\x08\x01" for frame in frames)
    assert all(frame[4:10] == bssid for frame in frames)
    assert all(frame[10:16] == our_mac for frame in frames)
    assert {struct.unpack(">H", frame[24 + 8 + 20 + 2:24 + 8 + 20 + 4])[0] for frame in frames} == {5678, 20561}


def test_mikrotik_response_matches_winbox_or_neighbor_udp_port():
    bssid = str_to_mac("aa:bb:cc:dd:ee:ff")
    our_mac = str_to_mac("02:00:00:00:00:01")
    assert is_mikrotik_response(_fromds_frame(bssid, our_mac, _udp_ipv4(20561, 49000)))
    assert is_mikrotik_response(_fromds_frame(bssid, our_mac, _udp_ipv4(5678, 49000)))
    assert not is_mikrotik_response(_fromds_frame(bssid, our_mac, _udp_ipv4(53, 49000)))


def test_mikrotik_plaintext_frame_can_match_tods_or_fromds():
    bssid = str_to_mac("aa:bb:cc:dd:ee:ff")
    our_mac = str_to_mac("02:00:00:00:00:01")
    fromds = _fromds_frame(bssid, our_mac, _udp_ipv4(20561, 49000))
    tods = b"\x08\x01\x00\x00" + bssid + our_mac + b"\x00\x11\x22\x33\x44\x55" + b"\x00\x00" + _udp_ipv4(49000, 5678)
    protected = bytes([fromds[0], fromds[1] | 0x40]) + fromds[2:]
    assert is_mikrotik_plaintext_frame(fromds)
    assert is_mikrotik_plaintext_frame(tods)
    assert not is_mikrotik_plaintext_frame(protected)
