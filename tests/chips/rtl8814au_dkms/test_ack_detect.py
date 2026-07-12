"""rtl8814au_dkms TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a
MAC we inject as, and the inject wait-for-ack poll. No hardware — synthetic frames.

The 8814au decodes on the reader thread, so the tap lives in _read_once (raw frames), not
_dispatch (parsed dicts): the parser drops control frames like the ACK before _dispatch."""
import struct
import time
from unittest.mock import MagicMock

from wifit3.chips.rtl8814au_dkms.driver import Rtl8814auDkmsDriver


def _ack_buf(ra: bytes) -> bytes:
    """A bulk-IN buffer with one NORMAL_RX packet: a 14-B on-wire ACK to ``ra`` (10-B MPDU
    + 4-B HW FCS, which iter_frames strips). 24-B RX desc: rxdw0 pkt_len=14, no drvinfo/shift."""
    desc = bytearray(24)
    struct.pack_into("<I", desc, 0, 14)         # rxdw0: pkt_len=14, all flags clear
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                              # FC: ACK control subtype
    mpdu[4:10] = ra                            # addr1 / RA
    return bytes(desc) + bytes(mpdu) + b"\x00\x00\x00\x00"


def _driver() -> Rtl8814auDkmsDriver:
    return Rtl8814auDkmsDriver(MagicMock())


def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._ack_detect_on = True
    d.transport.bulk_in.return_value = _ack_buf(ra)
    assert d._read_once() is None       # the ACK is tapped, never yielded as a parsed frame
    assert d.acks_seen(ra) == 1
    assert d._all_acks_seen == 1
    assert ra in d._ack_last_ts


def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    d._ack_detect_on = True
    d.transport.bulk_in.return_value = _ack_buf(ra)
    assert d._read_once() is None
    assert d._all_acks_seen == 1        # seen on-channel
    assert d.acks_seen(ra) == 0         # but not one of ours
    assert d._ack_last_ts == {}


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d.transport.bulk_in.return_value = _ack_buf(ra)   # _ack_detect_on stays False
    assert d._read_once() is None       # ACK is a control frame -> the parser drops it anyway
    assert d._all_acks_seen == 0
    assert d.acks_seen(ra) == 0


async def test_await_ack_true_when_ts_fresh():
    d = _driver()
    ta = bytes.fromhex("020000000001")
    since = time.monotonic()
    d._ack_last_ts[ta] = since + 1.0            # ACK landed after `since`
    assert await d._await_ack(ta, since, 0.05) is True


async def test_await_ack_false_on_timeout():
    d = _driver()
    ta = bytes.fromhex("020000000001")
    since = time.monotonic()
    assert await d._await_ack(ta, since, 0.005) is False   # no ts recorded -> window elapses
