"""rtl8187 TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a MAC we
inject as, and the inject wait-for-ack poll. No hardware — synthetic frames.

The 8187L admits ACK control frames in monitor by default (RX_CONF_CTRL is set in
mac.configure_filter, = FIF_CONTROL), so ack-detect is a pure software flag."""
import struct
import time
from unittest.mock import MagicMock

from wifit3.chips.rtl8187.driver import RTL8187Driver


def _ack_buf(ra: bytes) -> bytes:
    """A bulk-IN URB with one 8187L frame: a 14-B on-wire ACK to ``ra`` (10-B MPDU + 4-B
    FCS, which parse_rx_urb strips) followed by the 16-B trailing rtl8187_rx_hdr. The
    trailer flags[0:11] hold the FCS-inclusive length (14) with the CRC-error bit clear."""
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                              # FC: ACK control subtype
    mpdu[4:10] = ra                            # addr1 / RA
    frame = bytes(mpdu) + b"\x00\x00\x00\x00"  # 10-B MPDU + 4-B on-air FCS
    # rx_hdr: <I flags, B noise, B signal, B agc, B reserved, Q mac_time>
    trailer = struct.pack("<IBBBBQ", 14, 0, 0, 0, 0, 0)
    return frame + trailer


def _driver() -> RTL8187Driver:
    d = RTL8187Driver(MagicMock())
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


def test_tap_counts_ack_to_our_mac():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._ack_detect_on = True
    d._rx_dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 1
    assert ra in d._ack_last_ts
    assert d._parsed == []          # an ACK is never handed to the frame parser


def test_tap_ignores_ack_to_foreign_mac():
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    d._ack_detect_on = True
    d._rx_dispatch(_ack_buf(ra))
    assert d.acks_seen(ra) == 0     # but not one of ours
    assert d._ack_last_ts == {}


def test_tap_off_by_default():
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._rx_dispatch(_ack_buf(ra))    # _ack_detect_on stays False
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
