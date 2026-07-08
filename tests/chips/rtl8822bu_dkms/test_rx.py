"""Hardware-free regression for the 8822bu_dkms RX buffer decode + jaguar2 RSSI.

RX is environment-dependent, so it is not covered by the byte-for-byte pcap differ.
These synthetic-buffer tests pin the rx_pkt_desc walk (FCS strip, C2H/crc/icv skip)
and, above all, the jgr2 PHY-status RSSI decode — including the fix that rejects
saturated pwdb bytes (HW-observed pwdb 111-145 -> impossible >0 dBm) instead of
letting them poison the per-AP mean.
"""
from wifit3.chips.rtl8822bu_dkms import rx
from wifit3.chips.rtl8822bu_dkms.rx import RSSI_FLOOR, RSSI_UNKNOWN, _decode_rssi, _path_rssi


def _desc(pkt_len, drvinfo_sz=0, shift_sz=0, crc=0, icv=0, physt=0, c2h=0):
    dw0 = ((pkt_len & 0x3FFF) | (crc << 14) | (icv << 15)
           | ((drvinfo_sz // 8) << 16) | (shift_sz << 24) | (physt << 26))
    dw2 = c2h << 28
    return (dw0.to_bytes(4, "little") + b"\x00" * 4        # dw0, dw1
            + dw2.to_bytes(4, "little") + b"\x00" * 12)    # dw2, dw3..dw5 -> 24 bytes


def _pkt(payload_with_fcs, drvinfo_sz=0, shift_sz=0, drvinfo=b"", **kw):
    dv = (drvinfo + b"\x00" * drvinfo_sz)[:drvinfo_sz]
    body = (_desc(len(payload_with_fcs), drvinfo_sz, shift_sz, **kw)
            + dv + b"\x00" * shift_sz + payload_with_fcs)
    return body + b"\x00" * ((-len(body)) % 8)


def _frames(buf):
    return [f for f, _ in rx.iter_frames(buf)]


# --- rx_pkt_desc walk -------------------------------------------------------

def test_single_frame_fcs_stripped():
    assert _frames(_pkt(b"WXYZ" + b"\xde\xad\xbe\xef")) == [b"WXYZ"]


def test_aggregation_with_drvinfo_and_shift():
    p1 = _pkt(b"AAAA" + b"\x00\x00\x00\x00", drvinfo_sz=16, shift_sz=2)
    p2 = _pkt(b"BBBBBB" + b"\x00\x00\x00\x00", drvinfo_sz=8)
    assert _frames(p1 + p2) == [b"AAAA", b"BBBBBB"]


def test_c2h_and_error_frames_skipped_walk_continues():
    c2h = _pkt(b"\x01\x02\x03\x04\x05\x06\x07\x08", c2h=1)
    crc = _pkt(b"BAD!" + b"\x00\x00\x00\x00", crc=1)
    icv = _pkt(b"BAD!" + b"\x00\x00\x00\x00", icv=1)
    good = _pkt(b"KEEP" + b"\x00\x00\x00\x00")
    assert _frames(c2h + crc + icv + good) == [b"KEEP"]


def test_no_physt_reports_unknown_rssi():
    (frame, rssi), = rx.iter_frames(_pkt(b"WXYZ" + b"\x00\x00\x00\x00", physt=0))
    assert frame == b"WXYZ" and rssi == RSSI_UNKNOWN


# --- jgr2 RSSI decode + saturated-pwdb rejection ----------------------------

def _cck(pwdb):
    return bytes([0, pwdb]) + b"\x00" * 30


def _ofdm(pwdb_a, pwdb_b):
    return bytes([1, pwdb_a, pwdb_b]) + b"\x00" * 29


def test_path_rssi_valid_and_rejected():
    assert _path_rssi(43) == -67            # 43 - 110
    assert _path_rssi(95) == -15            # strong but valid
    assert _path_rssi(254) == -112          # >=0x80 sentinel wraps to the floor (kept)
    assert _path_rssi(122) is None          # impossible +12 dBm -> rejected
    assert _path_rssi(145) is None          # impossible +35 dBm -> rejected


def test_cck_normal_and_sentinel():
    assert _decode_rssi(_cck(43)) == -67
    assert _decode_rssi(_cck(254)) == -112


def test_cck_saturated_pwdb_falls_to_floor():
    # HW-observed weak-bleed CCK beacon: pwdb 122 -> was +12 dBm, now floored.
    assert _decode_rssi(_cck(122)) == RSSI_FLOOR


def test_ofdm_takes_strongest_valid_path():
    assert _decode_rssi(_ofdm(34, 32)) == -76        # max(-76, -78)
    assert _decode_rssi(_ofdm(20, 34)) == -76        # path B stronger


def test_ofdm_bad_path_rescued_by_good_path():
    # path A garbage (130 -> +20), path B valid (35 -> -75): report path B, not +20.
    assert _decode_rssi(_ofdm(130, 35)) == -75
    assert _decode_rssi(_ofdm(122, 30)) == -80


def test_ofdm_all_paths_saturated_falls_to_floor():
    assert _decode_rssi(_ofdm(130, 140)) == RSSI_FLOOR


def test_rssi_through_iter_frames():
    (_, rssi), = rx.iter_frames(
        _pkt(b"WXYZ" + b"\x00\x00\x00\x00", drvinfo_sz=32, physt=1, drvinfo=_ofdm(130, 35)))
    assert rssi == -75
