"""The Scanner raises a non-blocking toast on a capture win, so a handshake /
PMKID / recovered key isn't silent while the user watches the table. Driven
through the real ScannerView via _log_capture_event (WEP_KEY needs no disk save
and exercises the key-ascii toast body)."""
import pytest

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.scanner import ScannerView
from wifit3.ui.capture_events import CaptureEvent, CaptureKind
from wifit3.engine.models import AccessPoint


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_scanner_capture_win_raises_toast():
    app = WifiteApp()
    async with app.run_test() as pilot:
        app.push_screen("scanner")
        await pilot.pause()
        scanner = app.screen
        assert isinstance(scanner, ScannerView)

        toasts: list = []
        scanner.notify = lambda msg, **kw: toasts.append((kw.get("title"), msg))

        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:06", ssid="dd-wrt", channel=6)
        ev = CaptureEvent(kind=CaptureKind.WEP_KEY, bssid=ap.bssid, ssid=ap.ssid,
                          value="6162636465")   # hex "abcde"
        scanner._log_capture_event(ev, ap)

        assert ("WEP key recovered", 'dd-wrt: 6162636465 = "abcde"') in toasts, toasts


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_scanner_withheld_handshake_is_log_only_no_toast():
    """A withheld EAP/OWE 4-way logs but must NOT toast (not a crackable win)."""
    app = WifiteApp()
    async with app.run_test() as pilot:
        app.push_screen("scanner")
        await pilot.pause()
        scanner = app.screen

        toasts: list = []
        scanner.notify = lambda msg, **kw: toasts.append((kw.get("title"), msg))

        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:07", ssid="CorpNet", channel=1)
        ev = CaptureEvent(kind=CaptureKind.UNCRACKABLE_HANDSHAKE, bssid=ap.bssid,
                          ssid=ap.ssid, client_mac="11:22:33:44:55:66", value="OWE")
        scanner._log_capture_event(ev, ap)

        assert toasts == [], toasts
