"""rtl8821au RX dispatch: the driver-specific decode wiring fed to the shared
RxReaderThread. _rx_dispatch decodes a bulk buffer (iter_bulk_frames) → parses
→ fires the rx callback. (The thread/loop hand-off itself is covered by
tests/chips/test_rx_reader.py.)"""
from unittest.mock import MagicMock

import wifit3.chips.rtl8821au.driver as drv


def test_rx_dispatch_decodes_parses_and_fires_callback(monkeypatch):
    d = drv.RTL8821AUDriver(MagicMock())
    monkeypatch.setattr(
        drv, "iter_bulk_frames",
        lambda buf: [(None, b"\x80mpdu", -42)] if buf == b"BULK" else [],
    )
    monkeypatch.setattr(
        drv.WlanFrameParser, "parse_80211_frame",
        staticmethod(lambda mpdu, rssi: {"type": "beacon", "rssi": rssi}),
    )
    got = []
    d.register_rx_callback(got.append)
    d._rx_dispatch(b"BULK")
    assert got == [{"type": "beacon", "rssi": -42}]


def test_rx_dispatch_no_callback_is_safe(monkeypatch):
    d = drv.RTL8821AUDriver(MagicMock())
    monkeypatch.setattr(drv, "iter_bulk_frames", lambda buf: [(None, b"x", -1)])
    monkeypatch.setattr(
        drv.WlanFrameParser, "parse_80211_frame",
        staticmethod(lambda mpdu, rssi: {"type": "beacon"}),
    )
    d._rx_dispatch(b"BULK")  # no callback registered → must not raise
