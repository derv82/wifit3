"""A filtered-out AP is still evicted when it expires."""
from contextlib import asynccontextmanager
import time

import pytest
from textual.app import App

from wifit3.models import AccessPoint
from wifit3.wlan.sink import WlanSink
from wifit3.persist.vault import Vault
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.filter import EncryptionFilter, ScanFilter
from wifit3.ui.screens.scanner import ScannerView


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


