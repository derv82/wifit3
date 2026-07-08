"""rt2800usb RX dispatch: driver-specific decode (parse_rx_urb) wiring fed to
the shared RxReaderThread. (Thread/loop hand-off: tests/chips/test_rx_reader.py.)"""
from unittest.mock import MagicMock

import wifit3.chips.rt2800usb.driver as drv


def _rx(has_fcs_error=False, mpdu=b"mpdu", rssi=-40):
    o = MagicMock()
    o.has_fcs_error = has_fcs_error
    o.mpdu = mpdu
    o.rssi_dbm = rssi
    return o


def test_rx_dispatch_parses_and_fires_callback(monkeypatch):
    d = drv.RT2800USBDriver(MagicMock())
    monkeypatch.setattr(drv, "parse_rx_urb",
                        lambda buf, rxwi_size, rssi_cal: _rx() if buf == b"BULK" else None)
    monkeypatch.setattr(
        drv.WlanFrameParser, "parse_80211_frame",
        staticmethod(lambda mpdu, rssi: {"type": "beacon", "rssi": rssi}),
    )
    got = []
    d.register_rx_callback(got.append)
    d._rx_dispatch(b"BULK")
    assert got == [{"type": "beacon", "rssi": -40}]


def test_rx_dispatch_drops_fcs_error(monkeypatch):
    d = drv.RT2800USBDriver(MagicMock())
    monkeypatch.setattr(drv, "parse_rx_urb", lambda buf, rxwi_size, rssi_cal: _rx(has_fcs_error=True))
    got = []
    d.register_rx_callback(got.append)
    d._rx_dispatch(b"BULK")
    assert got == []
