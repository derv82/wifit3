"""Hardware-free regression for the M3b-3 RX buffer decode.

The bulk-IN RX path is not covered by the byte-for-byte pcap differ (RX is
environment-dependent), so these synthetic-buffer tests pin the rx_pkt_desc field
extraction, the recvbuf2recvframe aggregation walk (FCS strip, C2H skip, crc/icv
skip-and-continue), and the PHY-status RSSI decode.
"""
from wifit3.chips.rtl8814au_dkms import rx


def _desc(pkt_len, drvinfo_sz=0, shift_sz=0, crc=0, icv=0, physt=0, rpt_sel=0,
          rate=0x0C):
    dw0 = ((pkt_len & 0x3FFF) | (crc << 14) | (icv << 15)
           | ((drvinfo_sz // 8) << 16) | (shift_sz << 24) | (physt << 26))
    dw2 = rpt_sel << 28
    dw3 = rate & 0x7F
    return (dw0.to_bytes(4, "little") + b"\x00" * 4         # dw0, dw1
            + dw2.to_bytes(4, "little") + dw3.to_bytes(4, "little")  # dw2, dw3
            + b"\x00" * 8)                                  # dw4, dw5 -> 24 bytes


def _pkt(payload_with_fcs, drvinfo_sz=0, shift_sz=0, drvinfo=b"", **kw):
    """One aggregated RX packet, padded to the 8-byte boundary."""
    dv = (drvinfo + b"\x00" * drvinfo_sz)[:drvinfo_sz]
    body = (_desc(len(payload_with_fcs), drvinfo_sz, shift_sz, **kw)
            + dv + b"\x00" * shift_sz + payload_with_fcs)
    return body + b"\x00" * ((-len(body)) % 8)


def _frames(buf):
    return [f for f, _ in rx.iter_frames(buf)]


def test_query_rx_desc_fields():
    d = rx.query_rx_desc(_desc(0x1234, drvinfo_sz=32, shift_sz=2, physt=1,
                               rpt_sel=1, rate=0x0C))
    assert d.pkt_len == 0x1234
    assert d.drvinfo_sz == 32 and d.shift_sz == 2
    assert d.physt and d.rpt_sel
    assert d.data_rate == 0x0C
    assert not d.crc_err and not d.icv_err


def test_single_frame_fcs_stripped():
    # payload = 4 data bytes + 4-byte FCS; the FCS is stripped.
    assert _frames(_pkt(b"WXYZ" + b"\xde\xad\xbe\xef")) == [b"WXYZ"]


def test_aggregation_with_drvinfo_and_shift():
    p1 = _pkt(b"AAAA" + b"\x00\x00\x00\x00", drvinfo_sz=16, shift_sz=2)
    p2 = _pkt(b"BBBBBB" + b"\x00\x00\x00\x00", drvinfo_sz=8)
    assert _frames(p1 + p2) == [b"AAAA", b"BBBBBB"]


def test_c2h_report_skipped_but_walk_continues():
    c2h = _pkt(b"\x01\x02\x03\x04\x05\x06\x07\x08", rpt_sel=1)
    normal = _pkt(b"DATA" + b"\x00\x00\x00\x00")
    assert _frames(c2h + normal) == [b"DATA"]


def test_crc_error_skipped_walk_continues():
    good = _pkt(b"GOOD" + b"\x00\x00\x00\x00")
    bad = _pkt(b"BAD!" + b"\x00\x00\x00\x00", crc=1)
    after = _pkt(b"KEEP" + b"\x00\x00\x00\x00")
    # the crc-error packet is skipped, but the walk continues -> `after` survives.
    assert _frames(good + bad + after) == [b"GOOD", b"KEEP"]


def test_icv_error_skipped_walk_continues():
    good = _pkt(b"GOOD" + b"\x00\x00\x00\x00")
    bad = _pkt(b"BAD!" + b"\x00\x00\x00\x00", icv=1)
    after = _pkt(b"KEEP" + b"\x00\x00\x00\x00")
    assert _frames(good + bad + after) == [b"GOOD", b"KEEP"]


def test_truncated_pkt_offset_stops():
    # pkt_len claims more than the buffer holds -> no frame, no crash.
    assert _frames(_desc(0x3FF) + b"\x00" * 4) == []


def test_no_physt_reports_unknown_rssi():
    (frame, rssi), = rx.iter_frames(_pkt(b"WXYZ" + b"\x00\x00\x00\x00", physt=0))
    assert frame == b"WXYZ" and rssi == rx._RSSI_UNKNOWN


def test_rssi_ofdm_from_pwdb_all():
    # OFDM (rate 0x0c): pwdb_all (drvinfo byte 4) = 100 -> ((100>>1)&0x7f)-110 = -60.
    dv = bytes([0, 0, 0, 0, 100, 0])
    (_, rssi), = rx.iter_frames(
        _pkt(b"WXYZ" + b"\x00\x00\x00\x00", drvinfo_sz=32, physt=1, rate=0x0C,
             drvinfo=dv))
    assert rssi == -60


def test_rssi_cck_from_agc_report():
    # CCK (rate 0): cck_agc (drvinfo byte 5) lna=3 vga=5 -> -8 - 2*5 = -18.
    dv = bytes([0, 0, 0, 0, 0, (3 << 5) | 5])
    (_, rssi), = rx.iter_frames(
        _pkt(b"WXYZ" + b"\x00\x00\x00\x00", drvinfo_sz=32, physt=1, rate=0x00,
             drvinfo=dv))
    assert rssi == -18


def test_decode_rssi_cck_lookup():
    # lna 7 / vga 0 -> -38; lna 2 / vga 3 -> -1 - 6 = -7; unknown lna -> 0.
    assert rx.decode_rssi(bytes([0, 0, 0, 0, 0, (7 << 5) | 0]), data_rate=0) == -38
    assert rx.decode_rssi(bytes([0, 0, 0, 0, 0, (2 << 5) | 3]), data_rate=3) == -7
    assert rx.decode_rssi(bytes([0, 0, 0, 0, 0, (4 << 5) | 0]), data_rate=0) == 0
