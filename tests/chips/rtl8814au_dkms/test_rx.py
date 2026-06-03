"""Hardware-free regression for the M3b-3a RX buffer decode.

The bulk-IN RX path is not covered by the byte-for-byte pcap differ (RX is
environment-dependent), so these synthetic-buffer tests pin the rx_pkt_desc field
extraction and the recvbuf2recvframe aggregation walk, incl. FCS stripping, C2H
skipping, and the crc/icv-error stop.
"""
from wifit3.chips.rtl8814au_dkms import rx


def _desc(pkt_len, drvinfo_sz=0, shift_sz=0, crc=0, icv=0, physt=0, rpt_sel=0):
    dw0 = ((pkt_len & 0x3FFF) | (crc << 14) | (icv << 15)
           | ((drvinfo_sz // 8) << 16) | (shift_sz << 24) | (physt << 26))
    dw2 = rpt_sel << 28
    return (dw0.to_bytes(4, "little") + b"\x00" * 4
            + dw2.to_bytes(4, "little") + b"\x00" * 12)   # 24-byte desc


def _pkt(payload_with_fcs, drvinfo_sz=0, shift_sz=0, **kw):
    """One aggregated RX packet, padded to the 8-byte boundary."""
    body = (_desc(len(payload_with_fcs), drvinfo_sz, shift_sz, **kw)
            + b"\x00" * drvinfo_sz + b"\x00" * shift_sz + payload_with_fcs)
    return body + b"\x00" * ((-len(body)) % 8)


def test_query_rx_desc_fields():
    d = rx.query_rx_desc(_desc(0x1234, drvinfo_sz=32, shift_sz=2, physt=1, rpt_sel=1))
    assert d.pkt_len == 0x1234
    assert d.drvinfo_sz == 32 and d.shift_sz == 2
    assert d.physt and d.rpt_sel
    assert not d.crc_err and not d.icv_err


def test_single_frame_fcs_stripped():
    # payload = 4 data bytes + 4-byte FCS; the FCS is stripped.
    frames = list(rx.iter_frames(_pkt(b"WXYZ" + b"\xde\xad\xbe\xef")))
    assert frames == [b"WXYZ"]


def test_aggregation_with_drvinfo_and_shift():
    p1 = _pkt(b"AAAA" + b"\x00\x00\x00\x00", drvinfo_sz=16, shift_sz=2)
    p2 = _pkt(b"BBBBBB" + b"\x00\x00\x00\x00", drvinfo_sz=8)
    assert list(rx.iter_frames(p1 + p2)) == [b"AAAA", b"BBBBBB"]


def test_c2h_report_skipped_but_walk_continues():
    c2h = _pkt(b"\x01\x02\x03\x04\x05\x06\x07\x08", rpt_sel=1)
    normal = _pkt(b"DATA" + b"\x00\x00\x00\x00")
    assert list(rx.iter_frames(c2h + normal)) == [b"DATA"]


def test_crc_error_stops_walk():
    good = _pkt(b"GOOD" + b"\x00\x00\x00\x00")
    bad = _pkt(b"BAD!" + b"\x00\x00\x00\x00", crc=1)
    after = _pkt(b"LOST" + b"\x00\x00\x00\x00")
    # good is yielded; the crc-error packet ends the walk, dropping `after` too.
    assert list(rx.iter_frames(good + bad + after)) == [b"GOOD"]


def test_truncated_pkt_offset_stops():
    # pkt_len claims more than the buffer holds -> no frame, no crash.
    desc = _desc(0x3FF)            # huge pkt_len, tiny buffer
    assert list(rx.iter_frames(desc + b"\x00" * 4)) == []
