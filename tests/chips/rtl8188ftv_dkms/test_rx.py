"""rtl8188ftv_dkms RX: descriptor decode + aggregation walk."""

CAPDESC = bytes.fromhex("1400048400000f1074e140000030000000000000"
                        "006d350800")


def test_decode_desc():
    from wifit3.chips.rtl8188ftv_dkms import rx
    a = rx.decode_desc(CAPDESC)
    assert a["pkt_len"] == 20
    assert a["drvinfo_sz"] == 32
    assert a["shift_sz"] == 0
    assert (a["crc_err"], a["icv_err"], a["c2h"]) == (0, 0, 0)
    assert a["agg_pktnum"] == 0x00


def test_iter_rx_two_packets():
    import struct
    from wifit3.chips.rtl8188ftv_dkms import rx

    def desc(n):
        return struct.pack("<IIIIII", n, 0, 0, 0, 0, 0)

    p1 = b"\xb4\x00" + bytes(18)
    p2 = b"\xc4\x00" + bytes(12)
    buf = desc(20) + p1 + bytes(4) + desc(14) + p2 + bytes(2)
    pkts = list(rx.iter_rx(buf))
    assert [a["pkt_len"] for a, _ in pkts] == [20, 14]
    assert pkts[0][1][:2] == b"\xb4\x00"
    assert pkts[1][1][:2] == b"\xc4\x00"


def test_iter_rx_stops_on_crc():
    import struct
    from wifit3.chips.rtl8188ftv_dkms import rx
    bad = struct.pack("<I", 0x4000) + bytes(20)
    assert list(rx.iter_rx(CAPDESC + bytes(32) + bytes(20) + bad)) != []
    got = list(rx.iter_rx(bad + CAPDESC + bytes(32) + bytes(20)))
    assert got == []


def test_cck_rssi_table():
    from wifit3.chips.rtl8188ftv_dkms import rx
    assert rx.cck_rssi_dbm(0x72) == -56
    assert rx.cck_rssi_dbm((7 << 5) | 27) == -100
    assert rx.cck_rssi_dbm((1 << 5) | 19) == -44
    assert rx.cck_rssi_dbm(0x00) == 0


def test_signal_dbm_ofdm():
    from wifit3.chips.rtl8188ftv_dkms import rx
    assert rx.signal_dbm(bytes([0, 0, 0, 0, 0x78, 0]), 0x04) == -50
    assert rx.signal_dbm(bytes([0]), 0x04) is None


class _FltT:
    """RXFLTMAP1 read-modify-write fake: one 16-bit register at 0x06A2."""
    def __init__(self, val):
        self.val = val
        self.writes = []

    def read16(self, addr):
        assert addr == 0x06A2
        return self.val

    def write16(self, addr, value):
        self.writes.append((addr, value))
        self.val = value


def test_admit_and_drop_ack_frames():
    from wifit3.chips.rtl8188ftv_dkms import rx
    t = _FltT(0x0400)                       # post-monitor-entry default (PS-Poll only)
    rx.admit_ack_frames(t)
    assert t.writes[-1] == (0x06A2, 0x0400 | (1 << 13))   # ACK bit set, PS-Poll kept
    rx.drop_ack_frames(t)
    assert t.writes[-1] == (0x06A2, 0x0400)               # ACK bit cleared, PS-Poll kept
