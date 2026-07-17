"""rtl8188eus TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a MAC we
inject as. No hardware — synthetic frames.

The tap lives in _rx_dispatch (raw MPDUs), before the parser drops the ACK control frame. The
tally and arming live on the Driver base (record_ack / enable_rx_acks / acks_seen); _enable_rx_acks
opens RXFLTMAP1 bit13 (the 8188e otherwise leaves RXFLTMAP default). Frames are fed via a
monkeypatched iter_bulk_frames, matching the local rx_dispatch tests."""
from unittest.mock import MagicMock

import wifit3.chips.rtl8188eus.driver as drv


def _ack_mpdu(ra: bytes) -> bytes:
    """A 10-B FCS-stripped ACK (FC 0xD4, duration, RA) as iter_bulk_frames yields it."""
    return b"\xd4\x00\x00\x00" + ra


def _driver() -> drv.RTL8188EUSDriver:
    d = drv.RTL8188EUSDriver(MagicMock())
    d._parsed = []
    d.register_rx_callback(d._parsed.append)
    return d


def test_tap_counts_ack_to_our_mac(monkeypatch):
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    d._ack_detect_on = True                     # base state: the tally is armed
    monkeypatch.setattr(drv, "iter_bulk_frames", lambda buf: [(None, _ack_mpdu(ra), -40)])
    d._rx_dispatch(b"BULK")
    assert d.acks_seen(ra) == 1
    assert d._parsed == []          # an ACK is never handed to the frame parser


def test_tap_ignores_ack_to_foreign_mac(monkeypatch):
    d = _driver()
    ra = bytes.fromhex("aabbccddeeff")
    d._ack_detect_on = True
    monkeypatch.setattr(drv, "iter_bulk_frames", lambda buf: [(None, _ack_mpdu(ra), -40)])
    d._rx_dispatch(b"BULK")
    assert d.acks_seen(ra) == 0     # armed, but ra is not one of ours


def test_tap_off_by_default(monkeypatch):
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    monkeypatch.setattr(drv, "iter_bulk_frames", lambda buf: [(None, _ack_mpdu(ra), -40)])
    d._rx_dispatch(b"BULK")         # _ack_detect_on stays False -> record_ack is a no-op
    assert d.acks_seen(ra) == 0


def test_stamp_tx_seq_is_identity():
    d = _driver()
    frame = (b"\xc0\x00\x00\x00" + b"\xff" * 6 + bytes.fromhex("020000000001") + b"\x00" * 8)
    assert d._stamp_tx_seq(frame) is frame   # Realtek HW-stamps; frame goes out unchanged
