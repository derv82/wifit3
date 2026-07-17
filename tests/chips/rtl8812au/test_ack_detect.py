"""rtl8812au TX-ACK detection: the RX tap that counts the AP's link-layer ACKs to a MAC we
inject as, plus the inject descriptor's HW ACK-retry wiring. No hardware — synthetic frames.

The tap lives in _rx_dispatch (raw MPDUs), before the parser drops the ACK control frame.
Frames are fed via a monkeypatched iter_bulk_frames. ``_enable_rx_acks`` opens RXFLTMAP1 bit13
(exercised separately by mac_test coverage); the tally and arming live on the ``Driver`` base
(``record_ack`` / ``enable_rx_acks`` / ``acks_seen``)."""
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
    d._ack_detect_on = True             # arm the base tally (bypasses the RXFLTMAP1 register write)
    d._our_tx_macs.add(ra)
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
    assert d.acks_seen(ra) == 0     # armed, but not one of ours


def test_tap_off_by_default(monkeypatch):
    d = _driver()
    ra = bytes.fromhex("020000000001")
    d._our_tx_macs.add(ra)
    monkeypatch.setattr(drv, "iter_bulk_frames", lambda buf: [(None, _ack_mpdu(ra), -40)])
    monkeypatch.setattr(drv.WlanFrameParser, "parse_80211_frame",
                        staticmethod(lambda mpdu, rssi: None))  # parser drops the ctrl frame
    d._rx_dispatch(b"BULK")         # _ack_detect_on stays False
    assert d.acks_seen(ra) == 0


def test_stamp_tx_seq_is_identity():
    d = _driver()
    frame = b"\xc0\x00\x00\x00" + b"\x11" * 6 + b"\x22" * 6 + b"\x33" * 6 + b"\x00\x00"
    assert d._stamp_tx_seq(frame) is frame      # Realtek HW-stamps; frame goes out unchanged


async def test_inject_builds_descriptor_with_hw_retry_limit(monkeypatch):
    d = _driver()
    d._bulk_out_eps = [0x02, 0x03, 0x04]
    sent: list[bytes] = []

    def _fake_write(dev, ep, payload, timeout_ms=200):
        sent.append(bytes(payload))
        return len(payload)

    monkeypatch.setattr(drv, "write_bulk", _fake_write)   # module-level ref used by _inject_frame
    frame = b"\xc0\x00\x00\x00" + b"\x11" * 6 + b"\x22" * 6 + b"\x33" * 6 + b"\x00\x00"
    assert await d.inject_frame(frame) is True
    assert len(sent) == 1
    desc = sent[0][:40]
    rty = (int.from_bytes(desc[0x10:0x14], "little") >> 18) & 0x3F   # DATA_RT_LMT
    assert rty == d.DEFAULT_HW_ACK_RETRIES
    assert sent[0][40:] == frame                # HW-stamp: frame appended unchanged
