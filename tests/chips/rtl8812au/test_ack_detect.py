"""rtl8812au TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a MAC we
inject as, and the inject wait-for-ack poll. No hardware — synthetic frames.

The tap lives in _rx_dispatch (raw MPDUs), before the parser drops the ACK control frame.
Frames are fed via a monkeypatched iter_bulk_frames, matching the local rx_dispatch tests
(RXFLTMAP1 bit13 is opened by enable_ack_detect, exercised separately by mac_test coverage)."""
import time
from unittest.mock import MagicMock

import wifit3.chips.rtl8812au.driver as drv


def _ack_mpdu(ra: bytes) -> bytes:
    """A 10-B FCS-stripped ACK (FC 0xD4, duration, RA) as iter_bulk_frames yields it."""
    return b"\xd4\x00\x00\x00" + ra


def _driver() -> drv.RTL8812AUDriver:
    d = drv.RTL8812AUDriver(MagicMock())
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


def test_tap_counts_ack_to_our_mac(monkeypatch):
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._ack_detect_on = True
    monkeypatch.setattr(drv, "iter_bulk_frames", lambda buf: [(None, _ack_mpdu(ra), -40)])
    d._rx_dispatch(b"BULK")
    assert d.acks_seen(ra) == 1
    assert d._all_acks_seen == 1
    assert ra in d._ack_last_ts
    assert d._parsed == []          # an ACK is never handed to the frame parser


def test_tap_ignores_ack_to_foreign_mac(monkeypatch):
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    d._ack_detect_on = True
    monkeypatch.setattr(drv, "iter_bulk_frames", lambda buf: [(None, _ack_mpdu(ra), -40)])
    d._rx_dispatch(b"BULK")
    assert d._all_acks_seen == 1    # seen on-channel
    assert d.acks_seen(ra) == 0     # but not one of ours
    assert d._ack_last_ts == {}


def test_tap_off_by_default(monkeypatch):
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    monkeypatch.setattr(drv, "iter_bulk_frames", lambda buf: [(None, _ack_mpdu(ra), -40)])
    monkeypatch.setattr(drv.WlanFrameParser, "parse_80211_frame",
                        staticmethod(lambda mpdu, rssi: None))  # parser drops the ctrl frame
    d._rx_dispatch(b"BULK")         # _ack_detect_on stays False
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
