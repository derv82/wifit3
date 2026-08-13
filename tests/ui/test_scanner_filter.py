"""The Scanner applies its ScanFilter as a display-only predicate: a filtered-out
AP loses its table row but keeps its registry entry, so widening the filter brings
it straight back without having to rediscover it."""
import pytest
from textual.widgets import Button, DataTable

from wifit3.models import AccessPoint
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.filter import EncryptionFilter, ScanFilter
from wifit3.ui.screens.scanner import ScannerView


class _FakeArray:
    members = []

    def __init__(self, aps, supported):
        self.access_points = {ap.bssid: ap for ap in aps}
        self.clients = {}
        self.forged_macs = set()
        self.supported_channels = supported

    def get_access_points(self):
        return list(self.access_points.values())

    async def start_hopping(self, channels=None, interval=0.25):
        pass

    async def stop_hopping(self):
        pass


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_encryption_filter_hides_rows_but_keeps_registry():
    open_ap = AccessPoint(bssid="aa:bb:cc:00:00:01", ssid="OpenNet", channel=1, encryption="OPEN")
    wpa2_ap = AccessPoint(bssid="aa:bb:cc:00:00:02", ssid="SecureNet", channel=1, akms=["PSK"])

    app = WifiteApp()
    async with app.run_test() as pilot:
        app.array = _FakeArray([open_ap, wpa2_ap], [1, 6, 11])
        app.push_screen("scanner")
        await pilot.pause(0)
        scanner = app.screen
        assert isinstance(scanner, ScannerView)
        table = scanner.query_one("#ap-table", DataTable)

        scanner.refresh_table()
        assert table.row_count == 2

        scanner._scan_filter = ScanFilter(encryption=EncryptionFilter.WPA)
        scanner.refresh_table()
        assert table.row_count == 1
        assert wpa2_ap.bssid in scanner.ap_cache
        assert open_ap.bssid not in scanner.ap_cache
        # Display-only: the hidden AP is still in the registry.
        assert open_ap.bssid in app.array.access_points

        scanner._scan_filter = ScanFilter()
        scanner.refresh_table()
        assert table.row_count == 2
        assert open_ap.bssid in scanner.ap_cache


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_text_filter_matches_hidden_ap_via_guessed_sibling():
    named = AccessPoint(bssid="aa:bb:cc:00:00:10", ssid="Castle Crasher", channel=6,
                        akms=["PSK"], beacons=50)
    hidden = AccessPoint(bssid="aa:bb:cc:00:00:11", ssid=None, channel=6,
                         akms=["PSK"], siblings=[named.bssid])
    other = AccessPoint(bssid="aa:bb:cc:00:00:12", ssid="OpenNet", channel=6, encryption="OPEN")

    app = WifiteApp()
    async with app.run_test() as pilot:
        app.array = _FakeArray([named, hidden, other], [1, 6, 11])
        app.push_screen("scanner")
        await pilot.pause(0)
        scanner = app.screen
        table = scanner.query_one("#ap-table", DataTable)

        scanner._scan_filter = ScanFilter(text="castle")
        scanner.refresh_table()
        assert table.row_count == 2
        assert named.bssid in scanner.ap_cache              # matches by its own SSID
        assert hidden.bssid in scanner.ap_cache             # matches via the guessed sibling name
        assert other.bssid not in scanner.ap_cache


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_channel_modal_returns_focus_to_table():
    app = WifiteApp()
    async with app.run_test() as pilot:
        app.array = _FakeArray([], [1, 6, 11, 36, 40])
        app.push_screen("scanner")
        await pilot.pause(0)
        scanner = app.screen
        table = scanner.query_one("#ap-table", DataTable)
        scanner.query_one("#filter-channels", Button).focus()   # button holds focus, as in the app
        await pilot.pause()
        scanner.action_change_channel()
        await pilot.pause()
        app.screen.dismiss([1, 6])                              # confirm the dialog
        for _ in range(2):
            await pilot.pause()
        assert app.focused is table
