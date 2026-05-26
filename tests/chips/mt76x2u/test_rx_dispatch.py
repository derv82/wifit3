"""mt76x2u RxDrainer dispatch: driver-specific decode (decode_urb) wiring fed
to the shared RxReaderThread. (Thread/loop hand-off: tests/chips/test_rx_reader.py.)"""
from unittest.mock import MagicMock

import wifit3.chips.mt76x2u.rx as rx


def test_dispatch_decodes_and_fires_callback(monkeypatch):
    drainer = rx.RxDrainer(MagicMock())
    monkeypatch.setattr(
        rx, "decode_urb",
        lambda buf: {"is_beacon": True, "type": "beacon"} if buf == b"BULK" else None,
    )
    got = []
    drainer.frame_callback = got.append
    drainer._dispatch(b"BULK")
    assert got == [{"is_beacon": True, "type": "beacon"}]
    assert drainer.frames_decoded == 1
    assert drainer.beacon_count == 1


def test_dispatch_counts_decode_drop(monkeypatch):
    drainer = rx.RxDrainer(MagicMock())
    monkeypatch.setattr(rx, "decode_urb", lambda buf: None)
    got = []
    drainer.frame_callback = got.append
    drainer._dispatch(b"BULK")
    assert got == []
    assert drainer.frames_dropped == 1
