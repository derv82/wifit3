import struct

from wifit3.chips.rtl8192fu.rx import iter_frames, parse_descriptor, parse_phystats_rssi


def test_jaguar2_type0_new_agc_rssi():
    stats = bytes([0, 63]) + bytes(30)

    assert parse_phystats_rssi(stats, cck_new_agc=True) == -47


def test_jaguar2_type0_legacy_agc_rssi():
    stats = bytearray(32)
    stats[0] = 0
    stats[13] = (5 << 5) | 4

    assert parse_phystats_rssi(stats, cck_new_agc=False) == -36


def test_jaguar2_type1_uses_stronger_rx_path():
    stats = bytes([1, 42, 65]) + bytes(29)

    assert parse_phystats_rssi(stats) == -45


def test_jaguar2_type2_uses_stronger_rx_path():
    stats = bytes([2, 70, 51]) + bytes(29)

    assert parse_phystats_rssi(stats) == -40


def test_iter_frames_decodes_rxdesc24_phy_stats_and_eight_byte_alignment():
    frame = bytes.fromhex("80000000ffffffffffff0011223344550011223344550000")
    stats = bytes([1, 55, 61]) + bytes(29)
    word0 = len(frame) | (4 << 16) | (1 << 26)
    descriptor = struct.pack("<6I", word0, 0, 0, 4, 0, 0)
    packet = descriptor + stats + frame
    packet += bytes((-len(packet)) % 8)

    decoded = list(iter_frames(packet))

    assert len(decoded) == 1
    assert decoded[0] == (frame, -49)
    parsed = parse_descriptor(packet)
    assert parsed.driver_info_size == 32
    assert parsed.phy_stats is True
    assert parsed.rate == 4


def test_iter_frames_drops_crc_errors_and_reports():
    frame = bytes(24)
    crc_descriptor = struct.pack("<6I", len(frame) | (1 << 14), 0, 0, 0, 0, 0)
    report_descriptor = struct.pack("<6I", len(frame), 0, 1 << 28, 0, 0, 0)

    assert list(iter_frames(crc_descriptor + frame)) == []
    assert list(iter_frames(report_descriptor + frame)) == []
