"""The Scanner raises a non-blocking toast on a capture win, so a handshake /
PMKID / recovered key isn't silent while the user watches the table. Driven
through the real ScannerView's ApEventDetected handler."""
import pytest

from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.scanner import ScannerView
from wifit3.watchers.ap_watcher import UncrackableHandshakeSeen, WepKeyRecovered
from wifit3.models import AccessPoint
from wifit3.wlan.array import WlanArray


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_scanner_capture_win_raises_toast():
    app = WifiteApp()
    async with app.run_test() as pilot:
        app.push_screen("scanner")
        await pilot.pause(0)
        scanner = app.screen
        assert isinstance(scanner, ScannerView)

        toasts: list = []
        scanner.notify = lambda msg, **kw: toasts.append((kw.get("title"), msg))

        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:06", ssid="dd-wrt", channel=6)
        ev = WepKeyRecovered(bssid=ap.bssid, ssid=ap.ssid, channel=ap.channel,
                             key_hex="6162636465")   # "abcde"
        scanner.on_wifite_app_ap_event_detected(WifiteApp.ApEventDetected(ev))

        assert ("WEP key recovered", 'dd-wrt: 6162636465 = "abcde"') in toasts, toasts


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_scanner_withheld_handshake_is_log_only_no_toast():
    """A withheld EAP/OWE 4-way logs but must NOT toast (not a crackable win)."""
    app = WifiteApp()
    async with app.run_test() as pilot:
        app.push_screen("scanner")
        await pilot.pause(0)
        scanner = app.screen

        toasts: list = []
        scanner.notify = lambda msg, **kw: toasts.append((kw.get("title"), msg))

        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:07", ssid="CorpNet", channel=1)
        ev = UncrackableHandshakeSeen(bssid=ap.bssid, ssid=ap.ssid, channel=ap.channel,
                                      client_mac="11:22:33:44:55:66", akm_label="OWE")
        scanner.on_wifite_app_ap_event_detected(WifiteApp.ApEventDetected(ev))

        assert toasts == [], toasts


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_a_passive_decloak_reaches_disk():
    """Any reveal the sink makes (beacon leak, probe response, a reassoc after a deauth) must
    be offered as a candidate next launch, not just one the Decloak campaign won."""
    from wifit3.persist.config import Config, _PATH

    app = WifiteApp()
    async with app.run_test() as pilot:
        app.push_screen("scanner")
        await pilot.pause(0)

        array = WlanArray()
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:08", channel=11)
        array.access_points[ap.bssid] = ap
        app.array = array
        app._poll_ap_events()
        array.decloak(ap, "Songsong", "reassoc_req")
        app._poll_ap_events()

        assert Config.decloaked_ssids == {"aa:bb:cc:dd:ee:08": "Songsong"}
        assert "Songsong" in _PATH.read_text("utf-8")
