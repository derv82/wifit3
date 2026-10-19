"""The Scanner applies its ScanFilter as a display-only predicate: a filtered-out
AP loses its table row but keeps its registry entry, so widening the filter brings
it straight back without having to rediscover it."""
from contextlib import asynccontextmanager
import time

import pytest
import pytest_asyncio
from textual.app import App
from textual.widgets import Button, Checkbox, Input, Label

from wifit3.models import AccessPoint, IdKey, IdSource
from wifit3.persist.config import Config
from wifit3.persist.vault import Vault
from wifit3.ui.ap_table import APTable
from wifit3.ui.screens.filter import EncryptionFilter, FilterBar, ScanFilter
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
        self.clients = {}
        self.forged_macs = set()
        self.supported_channels = supported
        self.members = [_FakeIface(supported)] if supported else []
        self.stop_calls = 0
        self.start_calls = 0

    def get_access_points(self, include_eviltwin=True):
        return list(self.access_points.values())

    def remove_access_point(self, bssid):
        return self.access_points.pop(bssid, None)

    def select_iface(self, channel):
        return next((iface for iface in self.members if channel in iface.supported_channels), None)

    async def start_hopping(self, channels=None, interval=0.25):
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
    def __init__(self, array):
        super().__init__()
        self.array = array
        self.pbc_enabled = True
        self.vault = Vault()
        self.persist_calls = 0

    def persist_config(self) -> None:
        self.persist_calls += 1

    def on_mount(self) -> None:
        self.push_screen(ScannerView())


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "narrow"), [(83, True), (84, False)])
async def test_filter_bar_hides_encryption_label_below_84_columns(width, narrow):
    app = _ScannerHost(_FakeArray([], [1, 6, 11]))
    async with app.run_test(size=(width, 24)) as pilot:
        await pilot.pause(0)
        scanner = app.screen
        filter_bar = scanner.query_one(FilterBar)
        encryption_label = filter_bar.query_one(Label)
        encryption = scanner.query_one("#filter-encryption")
        channels = scanner.query_one("#filter-channels", Button)
        filter_input = scanner.query_one("#filter-text", Input)

        assert filter_input.region.y == encryption.region.y
        assert encryption_label.display is not narrow
        assert "Channels:" not in channels.label.plain


@pytest.mark.asyncio
async def test_filter_bar_scrolls_when_future_controls_exceed_its_width():
    app = _ScannerHost(_FakeArray([], [1, 6, 11]))
    async with app.run_test(size=(54, 24)) as pilot:
        await pilot.pause(0)
        filter_bar = app.screen.query_one(FilterBar)
        assert filter_bar.max_scroll_x > 0


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
async def test_hide_silenced_drops_the_row_but_keeps_the_registry_entry():
    silenced = AccessPoint(bssid="aa:bb:cc:00:00:01", ssid="NoisyNeighbour", channel=1, akms=["PSK"])
    audible = AccessPoint(bssid="aa:bb:cc:00:00:02", ssid="SecureNet", channel=1, akms=["PSK"])
    Config.silenced_bssids = [silenced.bssid]

    app = _ScannerHost(_FakeArray([silenced, audible], [1, 6, 11]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        table = scanner.query_one("#ap-table", APTable)

        scanner.refresh_table()
        assert table.row_count == 2          # off by default: silencing alone only badges the row

        scanner._scan_filter = ScanFilter(hide_silenced=True)
        scanner.refresh_table()
        assert table.row_count == 1
        assert silenced.bssid not in scanner.ap_cache
        assert silenced.bssid in app.array.access_points

        scanner._scan_filter = ScanFilter()
        scanner.refresh_table()
        assert table.row_count == 2
        assert silenced.bssid in scanner.ap_cache
        assert Config.is_silenced(silenced.bssid)   # un-ticking restores the row, not the silence


@pytest.mark.asyncio
async def test_hide_silenced_drops_a_row_silenced_after_the_box_was_ticked():
    """Silencing happens on Focus; the Scanner re-runs the predicate each tick, so the row goes."""
    ap = AccessPoint(bssid="aa:bb:cc:00:00:01", ssid="NoisyNeighbour", channel=1, akms=["PSK"])

    app = _ScannerHost(_FakeArray([ap], [1, 6, 11]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        table = scanner.query_one("#ap-table", APTable)

        scanner._scan_filter = ScanFilter(hide_silenced=True)
        scanner.refresh_table()
        assert table.row_count == 1

        Config.silenced_bssids = [ap.bssid]
        scanner.refresh_table()
        assert table.row_count == 0


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
        await pilot.pause()
        assert app.persist_calls == 1
        assert Config.hide_silenced is True

        scanner.query_one("#filter-text", Input).value = "netg"
        await pilot.pause()
        assert app.persist_calls == 1       # text edits leave the stored setting alone

        scanner.query_one("#filter-hide-silenced", Checkbox).value = False
        await pilot.pause()
        assert app.persist_calls == 2
        assert Config.hide_silenced is False


@pytest.mark.asyncio
async def test_text_filter_matches_hidden_ap_via_guessed_sibling():
    named = AccessPoint(bssid="aa:bb:cc:00:00:10", ssid="Castle Crasher", channel=6,
                        akms=["PSK"], beacons=50)
    hidden = AccessPoint(bssid="aa:bb:cc:00:00:11", ssid=None, channel=6,
                         akms=["PSK"], siblings=[named.bssid])
    other = AccessPoint(bssid="aa:bb:cc:00:00:12", ssid="OpenNet", channel=6, encryption="OPEN")

    app = _ScannerHost(_FakeArray([named, hidden, other], [1, 6, 11]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        table = scanner.query_one("#ap-table", APTable)

        scanner._scan_filter = ScanFilter(text="castle")
        scanner.refresh_table()
        assert table.row_count == 2
        assert named.bssid in scanner.ap_cache              # matches by its own SSID
        assert hidden.bssid in scanner.ap_cache             # matches via the guessed sibling name
        assert other.bssid not in scanner.ap_cache


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def _identity_app():
    app = _ScannerHost(_FakeArray([], [1, 6, 11]))
    async with app.run_test() as pilot:
        await pilot.pause(0)
        yield app, pilot


@pytest_asyncio.fixture(loop_scope="module")
async def identity_shown(_identity_app):
    """Returns the VENDOR/ID value the scanner hands the table for one AP."""
    app, pilot = _identity_app

    def shown(ap: AccessPoint) -> str:
        app.array.access_points = {ap.bssid: ap}
        app.screen.refresh_table()
        return app.screen.query_one("#ap-table", APTable).row_data(ap.bssid).identity

    return shown


@pytest.mark.asyncio(loop_scope="module")
async def test_scanner_identity_cell_shows_manufacturer_and_model(identity_shown):
    ap = AccessPoint(bssid="02:00:00:00:00:01", ssid="Lab", channel=1)
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "MikroTik")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "hAP ac²")
    assert identity_shown(ap) == "MikroTik hAP ac²"


@pytest.mark.asyncio(loop_scope="module")
async def test_scanner_identity_cell_blank_when_unknown(identity_shown):
    assert identity_shown(AccessPoint(bssid="02:00:00:00:00:01")) == ""


@pytest.mark.asyncio(loop_scope="module")
async def test_scanner_identity_cell_shows_summary(identity_shown):
    ap = AccessPoint(bssid="02:00:00:00:00:01", ssid="Vodafone-123456")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "Celeno")
    assert identity_shown(ap) == "Celeno"


@pytest.mark.asyncio(loop_scope="module")
async def test_scanner_identity_cell_oui_fallback(identity_shown):
    ap = AccessPoint(bssid="00:03:93:11:22:33", ssid="Alice’s iPhone")
    assert identity_shown(ap) == "Apple"
