"""ScannerView: a resort with nothing moved is a no-op, and forgetting a row drops the AP and
its clients."""
from typing import List, Optional
import pytest
from textual.app import App

from wifit3.models import AccessPoint
from wifit3.wlan.sink import WlanSink
from wifit3.persist.config import Config
from wifit3.persist.vault import Vault
from wifit3.ui.ap_table import APTable
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.scanner import ScannerView


@pytest.fixture(autouse=True)
def _zero_sort_delay(monkeypatch):
    monkeypatch.setattr(Config, "scanner_sort_delay", 0.0)


def _make_ap(
    bssid: str,
    ssid: Optional[str] = None,
    signal: int = -100,
    beacons: int = 0,
    wps: bool = False,
    wps_locked: bool = False,
) -> AccessPoint:
    ap = AccessPoint(
        bssid=bssid,
        ssid=ssid,
        beacons=beacons,
        wps=wps,
        wps_locked=wps_locked,
    )
    ap.signal_by_card = {"card0": signal}
    return ap


class _FakeIface:
    def __init__(self, supported: List[int]):
        self.supported_channels = supported
        self.current_channel = supported[0] if supported else 1
        self.chipset = "test"
        self._is_hopping = True

    async def stop_hopping(self) -> None:
        self._is_hopping = False

    async def start_hopping(self, channels=None, interval=0.25) -> None:
        self._is_hopping = True


class _FakeArray:
    def __init__(self, aps: List[AccessPoint], supported: List[int]):
        self.access_points = {ap.bssid: ap for ap in aps}
        self._sink = WlanSink()
        self._sink.access_points = self.access_points
        self.clients = {}
        self.removed_access_points = []
        self.forged_macs = set()
        self.supported_channels = supported
        self.members = [_FakeIface(supported)] if supported else []

    def guessid(self, ap):
        return self._sink.guessid(ap)

    def get_access_points(self, include_eviltwin: bool = True) -> List[AccessPoint]:
        return list(self.access_points.values())

    def remove_access_point(self, bssid: str) -> Optional[AccessPoint]:
        self.removed_access_points.append(bssid)
        ap = self.access_points.pop(bssid, None)
        self.clients = {
            mac: client for mac, client in self.clients.items()
            if client.bssid != bssid
        }
        return ap

    async def start_hopping(self, channels=None, interval=0.25, include_dfs_channels=True) -> None:
        pass

    async def stop_hopping(self) -> None:
        pass


class _ScannerHost(App):
    guessid = WifiteApp.guessid

    def __init__(self, array: _FakeArray):
        super().__init__()
        self.array = array
        self.pbc_enabled = True
        self.target_ap = None
        self.vault = Vault()

    def persist_config(self) -> None:
        pass

    def on_mount(self) -> None:
        self.push_screen(ScannerView())


@pytest.mark.asyncio
async def test_sort_aps_short_circuits_when_order_unchanged():
    ap1 = _make_ap("aa:bb:cc:00:00:01", ssid="A", signal=-50)
    ap2 = _make_ap("aa:bb:cc:00:00:02", ssid="B", signal=-60)

    app = _ScannerHost(_FakeArray([ap1, ap2], [1, 6, 11]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        table = scanner.query_one("#ap-table", APTable)

        scanner.refresh_table()

        # Re-running sort when data has not moved returns False (no-op short circuit)
        assert table.resort() is False


@pytest.mark.asyncio
async def test_forget_row_evicts_ap_and_its_clients():
    from wifit3.models import Client
    ap1 = _make_ap("aa:bb:cc:00:00:01", ssid="AP1", signal=-40)
    ap2 = _make_ap("aa:bb:cc:00:00:02", ssid="AP2", signal=-60)
    c1 = Client(mac="11:22:33:44:55:01", bssid=ap1.bssid)
    c2 = Client(mac="11:22:33:44:55:02", bssid=ap2.bssid)

    fake_array = _FakeArray([ap1, ap2], [1, 6, 11])
    fake_array.clients = {c1.mac: c1, c2.mac: c2}
    app = _ScannerHost(fake_array)
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        scanner.refresh_table()

        # Evict ap1 from scanner and array
        scanner._forget_row(ap1.bssid, drop_from_array=True)

        assert fake_array.removed_access_points == [ap1.bssid]
        assert ap1.bssid not in fake_array.access_points
        assert ap1.bssid not in scanner.ap_cache
        # c1 was associated with ap1, must be pruned
        assert c1.mac not in fake_array.clients
        # c2 was associated with ap2, must NOT be pruned
        assert c2.mac in fake_array.clients
