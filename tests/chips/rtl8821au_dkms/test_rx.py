import struct

from wifit3.chips.rtl8821au_dkms import rx


def _desc(pkt_len: int, *, crc: int = 0, icv: int = 0, drvinfo_sz: int = 0,
          shift_sz: int = 0, physt: int = 0, rpt_sel: int = 0, rate: int = 0) -> bytes:
    desc = bytearray(rx.RXDESC_SIZE)
    dw0 = (pkt_len & 0x3FFF) | (crc << 14) | (icv << 15) | ((drvinfo_sz // 8) << 16)
    dw0 |= (shift_sz & 0x3) << 24
    dw0 |= (physt & 0x1) << 26
    dw2 = (rpt_sel & 0x1) << 28
    dw3 = rate & 0x7F
    struct.pack_into("<I", desc, 0, dw0)
    struct.pack_into("<I", desc, 8, dw2)
    struct.pack_into("<I", desc, 12, dw3)
    return bytes(desc)


def _pkt(mpdu_with_fcs: bytes, *, drvinfo_sz: int = 0, shift_sz: int = 0,
         physt: int = 0, rate: int = 0, drvinfo: bytes = b"") -> bytes:
    drvinfo = drvinfo.ljust(drvinfo_sz, b"\0")
    return (_desc(len(mpdu_with_fcs), drvinfo_sz=drvinfo_sz, shift_sz=shift_sz,
                  physt=physt, rate=rate)
            + drvinfo + (b"\0" * shift_sz) + mpdu_with_fcs)


def test_query_rx_desc_fields():
    d = rx.query_rx_desc(_desc(0x1234, crc=1, icv=1, drvinfo_sz=32, shift_sz=2,
                               physt=1, rpt_sel=1, rate=0x0C))
    assert d.pkt_len == 0x1234
    assert d.crc_err and d.icv_err
    assert d.drvinfo_sz == 32 and d.shift_sz == 2
    assert d.physt and d.rpt_sel
    assert d.data_rate == 0x0C


def test_iter_frames_single_strips_fcs():
    mpdu = bytes(range(30))
    out = list(rx.iter_frames(_pkt(mpdu + b"\xde\xad\xbe\xef")))
    assert out == [(mpdu, rx._RSSI_UNKNOWN)]


def test_iter_frames_skip_crc_then_continue():
    good = bytes(range(40))
    bad = bytes(range(20))
    bad_pkt = _desc(len(bad) + rx.FCS_LEN, crc=1) + bad + b"\0" * rx.FCS_LEN
    good_pkt = _pkt(good + b"\0" * rx.FCS_LEN)
    frames = [f for f, _ in rx.iter_frames(bad_pkt + good_pkt)]
    assert frames == [good]


def test_iter_frames_skips_firmware_report():
    body = bytes(range(24))
    pkt = _desc(len(body) + rx.FCS_LEN, rpt_sel=1) + body + b"\0" * rx.FCS_LEN
    assert list(rx.iter_frames(pkt)) == []


def test_iter_frames_uses_drvinfo_for_rssi():
    mpdu = b"WXYZ"
    drvinfo = bytes([0, 0, 0, 0, 0x80, 0])
    (frame, rssi), = rx.iter_frames(
        _pkt(mpdu + b"\0" * rx.FCS_LEN, drvinfo_sz=32, physt=1, rate=4, drvinfo=drvinfo))
    assert frame == mpdu
    assert rssi == 0x40 - 110
