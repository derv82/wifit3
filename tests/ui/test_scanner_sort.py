"""ScannerView sorting contract: real-time deterministic sorting, WPS hierarchy,
and tie-breaking.
"""
from typing import List, Optional
import pytest
from textual.app import App
from textual.widgets import DataTable

from wifit3.models import AccessPoint
from wifit3.persist.config import Config
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
        self.clients = {}
        self.forged_macs = set()
        self.supported_channels = supported
        self.members = [_FakeIface(supported)] if supported else []

    def get_access_points(self, include_eviltwin: bool = True) -> List[AccessPoint]:
        return list(self.access_points.values())

    async def start_hopping(self, channels=None, interval=0.25) -> None:
        pass

    async def stop_hopping(self) -> None:
        pass


class _ScannerHost(App):
    def __init__(self, array: _FakeArray):
        super().__init__()
        self.array = array
        self.pbc_enabled = True

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
        table = scanner.query_one("#ap-table", DataTable)

        scanner.refresh_table()

        # Re-running sort when data has not moved returns False (no-op short circuit)
        order_changed = table.sort_aps("signal", lambda bssid, val: (0, scanner.ap_cache[bssid].signal), reverse=True)
        assert order_changed is False


@pytest.mark.asyncio
async def test_sort_channel_ascending_breaks_ties_with_stronger_power():
    # Two APs on channel 6: one strong (-35 dBm), one weak (-80 dBm).
    # One AP on channel 1 (-50 dBm).
    # Sorting by channel ascending: channel 1 first, then channel 6.
    # On channel 6, the stronger AP (-35) must be above the weaker AP (-80).
    ap_ch1 = _make_ap("aa:bb:cc:00:00:01", ssid="Net1", signal=-50)
    ap_ch1.channel = 1
    ap_ch6_weak = _make_ap("aa:bb:cc:00:00:02", ssid="Net6Weak", signal=-80)
    ap_ch6_weak.channel = 6
    ap_ch6_strong = _make_ap("aa:bb:cc:00:00:03", ssid="Net6Strong", signal=-35)
    ap_ch6_strong.channel = 6

    app = _ScannerHost(_FakeArray([ap_ch6_weak, ap_ch1, ap_ch6_strong], [1, 6, 11]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        table = scanner.query_one("#ap-table", DataTable)

        scanner._sort_idx = next(i for i, (col, _) in enumerate(scanner._COLUMNS) if col == "channel")
        scanner._sort_reverse = False  # Ascending

        scanner.refresh_table()

        ordered_keys = [r.value for r in list(table._row_locations)]
        assert ordered_keys == [
            "aa:bb:cc:00:00:01",  # Channel 1
            "aa:bb:cc:00:00:03",  # Channel 6 strong (-35)
            "aa:bb:cc:00:00:02",  # Channel 6 weak (-80)
        ]


@pytest.mark.asyncio
async def test_sort_ssid_ascending_sinks_hidden_networks():
    ap_b = _make_ap("aa:bb:cc:00:00:02", ssid="Bravo", signal=-50)
    ap_a = _make_ap("aa:bb:cc:00:00:01", ssid="Alpha", signal=-60)
    ap_hidden = _make_ap("aa:bb:cc:00:00:03", ssid=None, signal=-20)  # Strong but hidden

    app = _ScannerHost(_FakeArray([ap_b, ap_hidden, ap_a], [1, 6, 11]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        table = scanner.query_one("#ap-table", DataTable)

        scanner._sort_idx = next(i for i, (col, _) in enumerate(scanner._COLUMNS) if col == "ssid")
        scanner._sort_reverse = False  # Ascending

        scanner.refresh_table()

        ordered_keys = [r.value for r in list(table._row_locations)]
        assert ordered_keys == [
            "aa:bb:cc:00:00:01",  # Alpha
            "aa:bb:cc:00:00:02",  # Bravo
            "aa:bb:cc:00:00:03",  # Hidden sinks to bottom despite strong signal
        ]


@pytest.mark.asyncio
async def test_cursor_tracking_pins_highlight_on_resort():
    ap1 = _make_ap("aa:bb:cc:00:00:01", ssid="AP1", signal=-40)
    ap2 = _make_ap("aa:bb:cc:00:00:02", ssid="AP2", signal=-60)

    app = _ScannerHost(_FakeArray([ap1, ap2], [1, 6, 11]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        table = scanner.query_one("#ap-table", DataTable)

        scanner._sort_idx = next(i for i, (col, _) in enumerate(scanner._COLUMNS) if col == "signal")
        scanner._sort_reverse = True
        scanner.refresh_table()

        # Cursor starts on row 0 (ap1)
        assert table.cursor_coordinate.row == 0

        # Move cursor to ap2 (row 1)
        table.move_cursor(row=1, animate=False)
        assert table.cursor_coordinate.row == 1

        # Now ap2's signal jumps to -20 (stronger than ap1)
        ap2.signal_by_card["card0"] = -20
        scanner.refresh_table()

        # ap2 jumped to row 0, and cursor highlight must follow it to row 0
        assert [r.value for r in list(table._row_locations)] == [
            "aa:bb:cc:00:00:02",
            "aa:bb:cc:00:00:01",
        ]
        assert table.cursor_coordinate.row == 0


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

        assert ap1.bssid not in fake_array.access_points
        assert ap1.bssid not in scanner.ap_cache
        # c1 was associated with ap1, must be pruned
        assert c1.mac not in fake_array.clients
        # c2 was associated with ap2, must NOT be pruned
        assert c2.mac in fake_array.clients


