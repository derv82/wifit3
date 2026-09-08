"""The Scanner applies its ScanFilter as a display-only predicate: a filtered-out
AP loses its table row but keeps its registry entry, so widening the filter brings
it straight back without having to rediscover it."""
from contextlib import asynccontextmanager
import time

import pytest
from textual.widgets import Button, DataTable

from wifit3.campaigns.probe import ProbeResult
from wifit3.dot11.wsc.identity import WpsM1Identity
from wifit3.models import AccessPoint, IdKey, IdSource, PersistedCapture
from wifit3.persist.config import Config
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
        self.clients = {}
        self.forged_macs = set()
        self.supported_channels = supported
        self.members = [_FakeIface(supported)] if supported else []
        self.stop_calls = 0
        self.start_calls = 0

    def get_access_points(self, include_eviltwin=True):
        return list(self.access_points.values())

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


def test_scanner_has_identity_column():
    columns = [key for key, _label in ScannerView._COLUMNS]
    labels = dict(ScannerView._COLUMNS)
    assert "identity" in columns
    assert labels["identity"] == "IDENTITY"
    assert "brand" not in columns
    assert "kind" not in columns


def test_scanner_has_router_info_probe_keybind():
    assert any(binding.key == "p" and binding.action == "probe_router_info"
               for binding in ScannerView.BINDINGS)


def test_scanner_identity_cell_shows_manufacturer_and_model():
    scanner = ScannerView()
    scanner._theme_fg = "white"
    ap = AccessPoint(
        bssid="02:00:00:00:00:01",
        ssid="Lab",
        channel=1,
    )
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "MikroTik")
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MODEL_NAME, "hAP ac²")
    cell = scanner._identity_cell(ap)
    assert cell.plain == "MikroTik hAP ac²"


def test_scanner_identity_cell_blank_when_unknown():
    scanner = ScannerView()
    scanner._theme_fg = "white"
    ap = AccessPoint(bssid="02:00:00:00:00:01")
    assert scanner._identity_cell(ap).plain == ""


def test_scanner_identity_cell_shows_summary():
    scanner = ScannerView()
    scanner._theme_fg = "white"
    ap = AccessPoint(
        bssid="02:00:00:00:00:01",
        ssid="Vodafone-123456",
    )
    ap.identity.set(IdSource.WSC_BEACON, IdKey.MANUFACTURER, "Celeno")
    assert scanner._identity_cell(ap).plain == "Celeno"


def test_scanner_identity_cell_oui_fallback():
    scanner = ScannerView()
    scanner._theme_fg = "white"
    ap = AccessPoint(bssid="00:03:93:11:22:33", ssid="Alice’s iPhone")
    assert scanner._identity_cell(ap).plain == "Apple"


def test_scanner_freezes_all_row_ages_while_probing():
    scanner = ScannerView()
    start = time.time()
    later = start + 20
    probed = AccessPoint(bssid="aa:bb:cc:00:00:52", ssid="Router", channel=1)
    other = AccessPoint(bssid="aa:bb:cc:00:00:53", ssid="Router", channel=1)
    probed.last_seen = start - 5
    other.last_seen = start - 10
    scanner._router_info_probing = True
    scanner._router_info_probe_started_at = start
    scanner._router_info_probe_bssid = probed.bssid

    assert scanner._ap_row_age(probed, later) == 5
    assert scanner._ap_row_age(other, later) == 10


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_scanner_info_probe_updates_ap_identity(monkeypatch):
    ap = AccessPoint(bssid="aa:bb:cc:00:00:50", ssid="Router", channel=1, wps=True)
    async def fake_probe(iface, target):
        assert target is ap
        assert iface is not None
        ap.identity.set(IdSource.WSC_M1, IdKey.MANUFACTURER, "TP-Link")
        ap.identity.set(IdSource.WSC_M1, IdKey.MODEL_NAME, "Archer AX10")
        ap.identity.set(IdSource.WSC_M1, IdKey.DEVICE_NAME, "Office")
        return ProbeResult(
            True,
            source="wps.m1",
            wps_identity=WpsM1Identity(manufacturer="TP-Link", model_name="Archer AX10", device_name="Office"),
        )

    import wifit3.ui.screens.scanner as scanner_module

    monkeypatch.setattr(scanner_module, "probe_ap", fake_probe)
    app = WifiteApp()
    async with app.run_test() as pilot:
        app.array = _FakeArray([ap], [1, 6, 11])
        iface = app.array.members[0]
        app.push_screen("scanner")
        await pilot.pause(0)
        scanner = app.screen
        assert isinstance(scanner, ScannerView)
        toasts = []
        scanner.notify = lambda *args, **kwargs: toasts.append((args, kwargs))

        scanner.refresh_table()
        array_stop_calls = app.array.stop_calls
        array_start_calls = app.array.start_calls
        await scanner._probe_router_info(ap)

    assert app.array.stop_calls == array_stop_calls
    assert app.array.start_calls == array_start_calls
    assert iface.stop_calls == 1
    assert iface.start_calls == 1
    assert toasts == []
    assert ap.identity.manufacturer == "TP-Link"
    assert ap.identity.model_name == "Archer AX10"
    assert ap.identity.device_name == "Office"
    assert ap.identity.summary == "TP-Link Archer AX10"


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_usb_devices")
async def test_scanner_info_probe_updates_probe_identity(monkeypatch):
    ap = AccessPoint(bssid="aa:bb:cc:00:00:51", ssid="Router", channel=1)
    result = ProbeResult(
        True,
        source="mikrotik",
        vendor="MikroTik",
    )

    async def fake_probe(iface, target):
        assert target is ap
        assert iface is not None
        ap.identity.set(IdSource.WINBOX_PROBE, IdKey.MANUFACTURER, "MikroTik")
        return result

    import wifit3.ui.screens.scanner as scanner_module

    monkeypatch.setattr(scanner_module, "probe_ap", fake_probe)
    app = WifiteApp()
    async with app.run_test() as pilot:
        app.array = _FakeArray([ap], [1, 6, 11])
        app.push_screen("scanner")
        await pilot.pause(0)
        scanner = app.screen
        assert isinstance(scanner, ScannerView)
        toasts = []
        scanner.notify = lambda *args, **kwargs: toasts.append((args, kwargs))

        scanner.refresh_table()
        await scanner._probe_router_info(ap)

    assert toasts == []
    assert ap.identity.manufacturer == "MikroTik"
    assert scanner._identity_cell(ap).plain == "MikroTik"


def test_ssid_chips_zero_one_two(monkeypatch):
    ap = AccessPoint(bssid="aa:bb:cc:00:00:40", ssid="Net", channel=1)

    monkeypatch.setattr(Config, "silenced_bssids", [])
    assert ScannerView._ssid_chips_markup(ap) == ""

    monkeypatch.setattr(Config, "silenced_bssids", [ap.bssid])
    assert ScannerView._ssid_chips_markup(ap) == "[red]✗S[/red]"

    ap.persisted = [PersistedCapture(type="HS", timestamp=0, path="x")]
    assert ScannerView._ssid_chips_markup(ap) == "[red]✗S[/red] [green]✓HS[/green]"


def test_ssid_cell_clips_wide_ssid_to_cap(monkeypatch):
    """SSID width must be measured in display cells, not chars: a wide (2-cell)
    SSID over the cap gets clipped, and its leading chip survives the clip."""
    scanner = ScannerView()
    scanner._theme_fg = "white"
    ap = AccessPoint(bssid="aa:bb:cc:00:00:41", ssid="ネ" * 40, channel=1)  # 80 cells
    monkeypatch.setattr(Config, "silenced_bssids", [ap.bssid])

    cell = scanner._ssid_cell(ap)
    assert cell.cell_len <= ScannerView._SSID_CELL_MAX
    assert cell.plain.startswith("✗S")
    assert "…" in cell.plain
    assert cell.justify == "right"
