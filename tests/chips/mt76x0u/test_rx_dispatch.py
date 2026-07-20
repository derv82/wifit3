"""mt76x0u RxDrainer dispatch: the driver-specific decode (decode_rx_packet)
wiring fed to the shared RxReaderThread. (Thread/loop hand-off is covered by
tests/chips/test_rx_reader.py.)"""
from unittest.mock import MagicMock

import wifit3.chips.mt76x0u.rx as rx


def test_dispatch_decodes_parses_and_fires_callback(monkeypatch):
    drainer = rx.RxDrainer(MagicMock())
    rxobj = MagicMock()
    rxobj.frame = b"frame"
    rxobj.rssi_dbm = -40
    monkeypatch.setattr(rx, "decode_rx_packet",
                        lambda data: rxobj if data == b"BULK" else None)
    monkeypatch.setattr(
        "wifit3.dot11.parser.WlanFrameParser.parse_80211_frame",
        staticmethod(lambda frame, rssi: {"type": "beacon", "rssi": rssi}),
    )
    got = []
    drainer.frame_callback = got.append
    drainer._dispatch(b"BULK")
    assert got == [{"type": "beacon", "rssi": -40}]
    assert drainer.dispatched == 1


def test_dispatch_counts_decode_failure(monkeypatch):
    drainer = rx.RxDrainer(MagicMock())
    monkeypatch.setattr(rx, "decode_rx_packet", lambda data: None)
    got = []
    drainer.frame_callback = got.append
    drainer._dispatch(b"BULK")
    assert got == []
    assert drainer.decode_failures == 1
