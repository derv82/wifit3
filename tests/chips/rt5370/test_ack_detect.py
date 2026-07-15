"""rt5370 TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a
MAC we inject as, and the inject wait-for-ack poll. No hardware — synthetic frames.
The Ralink monitor RX filter (RX_FILTER_CFG=0x93) already admits ACKs, so arming is a
pure software flag; the tap keys off each MPDU iter_frames yields in _dispatch."""
import time
from unittest.mock import MagicMock

import wifit3.chips.rt5370.driver as drv


def _ack_mpdu(ra: bytes) -> bytes:
    """A 10-byte on-wire ACK to ``ra``: FC=0xD4 (control/ACK) + duration + addr1/RA."""
    mpdu = bytearray(10)
    mpdu[0] = 0xD4                              # FC: ACK control subtype
    mpdu[4:10] = ra                            # addr1 / RA
    return bytes(mpdu)


def _driver(monkeypatch, ra: bytes) -> drv.RT5370Driver:
    d = drv.RT5370Driver(MagicMock())
    monkeypatch.setattr(drv, "iter_frames", lambda buf, ev, lna: [(_ack_mpdu(ra), -40)])
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


def test_tap_counts_ack_to_our_mac(monkeypatch):
    ra = bytes.fromhex("020000000001")
    d = _driver(monkeypatch, ra)
    d._our_tx_macs.add(ra)
    d._ack_detect_on = True
    d._dispatch(b"BULK")
    assert d.acks_seen(ra) == 1
    assert ra in d._ack_last_ts
    assert d._parsed == []          # an ACK is never handed to the frame parser


def test_tap_ignores_ack_to_foreign_mac(monkeypatch):
    ra = bytes.fromhex("aabbccddeeff")
    d = _driver(monkeypatch, ra)
    d._ack_detect_on = True
    d._dispatch(b"BULK")
    assert d.acks_seen(ra) == 0     # but not one of ours
    assert d._ack_last_ts == {}


def test_tap_off_by_default(monkeypatch):
    ra = bytes.fromhex("020000000001")
    d = _driver(monkeypatch, ra)
    d._our_tx_macs.add(ra)
    d._dispatch(b"BULK")            # _ack_detect_on stays False
    assert d.acks_seen(ra) == 0


async def test_await_ack_true_when_ts_fresh():
    d = drv.RT5370Driver(MagicMock())
    ta = bytes.fromhex("020000000001")
    since = time.monotonic()
    d._ack_last_ts[ta] = since + 1.0            # ACK landed after `since`
    assert await d._await_ack(ta, since, 0.05) is True


async def test_await_ack_false_on_timeout():
    d = drv.RT5370Driver(MagicMock())
    ta = bytes.fromhex("020000000001")
    since = time.monotonic()
    assert await d._await_ack(ta, since, 0.005) is False   # no ts recorded -> window elapses
