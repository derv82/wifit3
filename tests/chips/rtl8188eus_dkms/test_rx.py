"""Hardware-free regression for the RTL8188EUS (DKMS) RX buffer decode.

Locks the 24-byte RX-desc field positions, the aggregation walk (skip-and-continue +
_RND4), FCS stripping, and the CCK/OFDM RSSI decode. End-to-end RX is hardware-validated
by a live beacon count (no pcap gate — RX is environment-dependent).
"""
from wifit3.chips.rtl8188eus_dkms import rx


def _desc(pkt_len, drvinfo_sz=0, shift_sz=0, physt=0, crc=0, icv=0, rpt=0, rate=4):
    dw0 = ((pkt_len & 0x3FFF) | (crc << 14) | (icv << 15)
           | ((drvinfo_sz // 8) << 16) | (shift_sz << 24) | (physt << 26))
    dw3 = (rate & 0x3F) | (rpt << 14)
    return (dw0.to_bytes(4, "little") + b"\0" * 8         # dw0, dw1, dw2
            + dw3.to_bytes(4, "little") + b"\0" * 8)      # dw3, dw4, dw5  (24 B total)


def test_query_rx_desc_fields():
    d = rx.query_rx_desc(_desc(0x20, drvinfo_sz=16, shift_sz=2, physt=1, rate=11))
    assert d.pkt_len == 0x20 and d.drvinfo_sz == 16 and d.shift_sz == 2
    assert d.physt and d.pkt_rpt_type == rx.NORMAL_RX and d.data_rate == 11
    assert not d.crc_err and not d.icv_err


def test_iter_frames_single_strips_fcs():
    mpdu = bytes(range(30))
    fcs = b"\xde\xad\xbe\xef"
    buf = _desc(len(mpdu) + 4) + mpdu + fcs
    out = list(rx.iter_frames(buf))
    assert len(out) == 1
    frame, rssi = out[0]
    assert frame == mpdu and rssi == rx._RSSI_UNKNOWN   # physt=0 -> unknown


def test_iter_frames_skip_crc_then_continue():
    good = bytes(range(40))
    bad = bytes(range(20))
    # bad (crc_err) is skipped but the walk continues to the good frame after it.
    buf = (_desc(len(bad) + 4, crc=1) + bad + b"\0\0\0\0"
           + _desc(len(good) + 4) + good + b"\0\0\0\0")
    frames = [f for f, _ in rx.iter_frames(buf)]
    assert frames == [good]


def test_iter_frames_skips_tx_report():
    # pkt_rpt_type != NORMAL_RX (a TX report) is not an 802.11 frame.
    body = bytes(range(24))
    buf = _desc(len(body) + 4, rpt=2) + body + b"\0\0\0\0"
    assert list(rx.iter_frames(buf)) == []


def test_decode_rssi_ofdm():
    # OFDM: byte4 pwdb -> ((pwdb>>1)&0x7f)-110. pwdb=0x80 -> (0x40)-110 = -46.
    ps = bytes([0, 0, 0, 0, 0x80, 0]) + b"\0" * 10
    assert rx.decode_rssi(ps, data_rate=4) == 0x40 - 110


def test_decode_rssi_cck():
    # CCK: byte5 -> LNA[7:5]/VGA[4:0]; lna_gain_table_1[LNA] - 2*VGA.
    # byte5=0x62 -> LNA=3 (gain 3), VGA=2 -> 3 - 4 = -1.
    ps = bytes([0, 0, 0, 0, 0, 0x62]) + b"\0" * 10
    assert rx.decode_rssi(ps, data_rate=1) == 3 - 4
