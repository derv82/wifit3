"""ScannerView's ApEventDetected handler, and the app's decloak persistence."""
import pytest

from wifit3.ui.app import WifiteApp
from wifit3.watchers.ap_watcher import UncrackableHandshakeSeen
from wifit3.models import AccessPoint
from wifit3.wlan.array import WlanArray


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


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_a_pbc_window_under_a_modal_opens_focus_when_the_modal_closes():
    from textual.screen import ModalScreen

    app = WifiteApp()
    async with app.run_test() as pilot:
        array = WlanArray()
        ap = AccessPoint(bssid="aa:bb:cc:dd:ee:09", ssid="PbcNet", channel=6)
        array.access_points[ap.bssid] = ap
        app.array = array
        app.pbc_enabled = True
        app.push_screen("scanner")
        await pilot.pause(0)
        scanner = app.screen
        modal = ModalScreen()
        app.push_screen(modal)
        await pilot.pause(0)

        ap.wps, ap.wps_selected_registrar, ap.wps_device_password_id = True, True, 0x0004
        app._poll_ap_events()
        await pilot.pause(0.1)
        assert app.screen is modal

        modal.dismiss()
        await pilot.pause(0.1)
        assert app.target_ap is ap
        assert app.screen is not scanner and app.screen is not modal
