"""The Scanner applies its ScanFilter as a display-only predicate: a filtered-out
AP loses its table row but keeps its registry entry, so widening the filter brings
it straight back without having to rediscover it."""
from contextlib import asynccontextmanager
import time

import pytest
from textual.app import App
from textual.widgets import Checkbox, Input

from wifit3.models import AccessPoint
from wifit3.wlan.sink import WlanSink
from wifit3.persist.config import Config
from wifit3.persist.vault import Vault
from wifit3.ui.ap_table import APTable
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.filter import EncryptionFilter, ScanFilter
from wifit3.ui.screens.scanner import ScannerView
from tests.ui.wait_until import wait_until


class _FakeIface:
    def __init__(self, supported):
        self.supported_channels = supported
        self.current_channel = supported[0] if supported else 1
        self.chipset = "test"
        self._is_hopping = True
        self.stop_calls = 0
        self.start_calls = 0

    async def stop_hopping(self):
        self.stop_calls += 1
        self._is_hopping = False

    async def start_hopping(self, channels=None, interval=0.25):
        self.start_calls += 1
        self._is_hopping = True


class _FakeArray:
    def __init__(self, aps, supported):
        self.access_points = {ap.bssid: ap for ap in aps}
        self._sink = WlanSink()
        self._sink.access_points = self.access_points
        self.clients = {}
        self.forged_macs = set()
        self.supported_channels = supported
        self.members = [_FakeIface(supported)] if supported else []
        self.stop_calls = 0
        self.start_calls = 0

    def guessid(self, ap):
        return self._sink.guessid(ap)

    def get_access_points(self, include_eviltwin=True):
        return list(self.access_points.values())

    def remove_access_point(self, bssid):
        return self.access_points.pop(bssid, None)

    def select_iface(self, channel):
        return next((iface for iface in self.members if channel in iface.supported_channels), None)

    async def start_hopping(self, channels=None, interval=0.25, include_dfs_channels=True):
        self.start_calls += 1

    async def stop_hopping(self):
        self.stop_calls += 1

    @asynccontextmanager
    async def claim(self, iface):
        await iface.stop_hopping()
        try:
            yield iface
        finally:
            await iface.start_hopping()


class _ScannerHost(App):
    CSS = WifiteApp.CSS
    guessid = WifiteApp.guessid

    def __init__(self, array):
        super().__init__()
        self.array = array
        self.pbc_enabled = True
        self.target_ap = None
        self.vault = Vault()
        self.persist_calls = 0

    def persist_config(self) -> None:
        self.persist_calls += 1

    def on_mount(self) -> None:
        self.push_screen(ScannerView())


@pytest.mark.asyncio
async def test_encryption_filter_hides_rows_but_keeps_registry():
    open_ap = AccessPoint(bssid="aa:bb:cc:00:00:01", ssid="OpenNet", channel=1, encryption="OPEN")
    wpa2_ap = AccessPoint(bssid="aa:bb:cc:00:00:02", ssid="SecureNet", channel=1, akms=["PSK"])

    app = _ScannerHost(_FakeArray([open_ap, wpa2_ap], [1, 6, 11]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        assert isinstance(scanner, ScannerView)
        table = scanner.query_one("#ap-table", APTable)

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
async def test_filtered_ap_is_evicted_after_it_expires():
    open_ap = AccessPoint(bssid="aa:bb:cc:00:00:01", ssid="OpenNet", channel=1, encryption="OPEN")
    app = _ScannerHost(_FakeArray([open_ap], [1, 6, 11]))

    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        scanner._scan_filter = ScanFilter(encryption=EncryptionFilter.WPA)
        open_ap.last_seen = time.time() - 31

        scanner.refresh_table()

        assert open_ap.bssid not in app.array.access_points
        assert open_ap.bssid not in scanner.ap_cache


@pytest.mark.asyncio
async def test_scanner_starts_with_the_persisted_hide_silenced_setting():
    silenced = AccessPoint(bssid="aa:bb:cc:00:00:01", ssid="NoisyNeighbour", channel=1, akms=["PSK"])
    audible = AccessPoint(bssid="aa:bb:cc:00:00:02", ssid="SecureNet", channel=1, akms=["PSK"])
    Config.silenced_bssids = [silenced.bssid]
    Config.hide_silenced = True

    app = _ScannerHost(_FakeArray([silenced, audible], [1, 6, 11]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        assert scanner._scan_filter.hide_silenced is True
        scanner.refresh_table()
        assert scanner.query_one("#ap-table", APTable).row_count == 1
        assert app.persist_calls == 0        # restoring the setting is not a change to write back


@pytest.mark.asyncio
async def test_only_the_checkbox_writes_config_not_every_keystroke():
    ap = AccessPoint(bssid="aa:bb:cc:00:00:02", ssid="SecureNet", channel=1, akms=["PSK"])

    app = _ScannerHost(_FakeArray([ap], [1, 6, 11]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        app.persist_calls = 0

        scanner.query_one("#filter-text", Input).value = "net"
        await pilot.pause()
        assert app.persist_calls == 0

        scanner.query_one("#filter-hide-silenced", Checkbox).value = True
        await wait_until(pilot, lambda: app.persist_calls == 1)
        assert Config.hide_silenced is True

        scanner.query_one("#filter-text", Input).value = "netg"
        await pilot.pause()
        assert app.persist_calls == 1       # text edits leave the stored setting alone

        scanner.query_one("#filter-hide-silenced", Checkbox).value = False
        await wait_until(pilot, lambda: app.persist_calls == 2)
        assert Config.hide_silenced is False
