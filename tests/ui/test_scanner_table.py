"""APTable contract: what it renders, and that its caches never lie about it.

The DataTable bugs this widget replaces were all "state correct, pixels wrong": a cache key
missing an input left a stale strip on screen while every widget-state assertion still passed.
So the central test here is differential — after each mutation the cached render must equal a
cold render of the same state. Any input missing from a cache key fails it immediately.
"""
from dataclasses import replace

import pytest
import pytest_asyncio
from textual.app import App, ComposeResult
from textual.screen import Screen

from wifit3.models import AccessPoint
from wifit3.persist.config import Config
from wifit3.persist.vault import Vault
from wifit3.ui.ap_table import COLUMN_KEYS, COLUMNS, APRow, APTable
from wifit3.ui.screens.scanner import ScannerView
from wifit3.ui.encryption_format import EncryptionSummary, EncryptionType

_WPA2 = EncryptionSummary(type=EncryptionType.WPA2, akms="PSK", cipher="CCMP",
                          wep_ivs=0, raw="WPA2")
_OPEN = EncryptionSummary(type=EncryptionType.OPEN, akms="", cipher=None,
                          wep_ivs=0, raw="OPEN")


def _row(index: int, **overrides) -> APRow:
    defaults = dict(
        bssid=f"02:00:00:00:{index // 256:02x}:{index % 256:02x}",
        ssid=f"Net{index}",
        channel=1 + (index % 11),
        signal=-30 - (index % 60),
        beacons=index * 3,
        clients=index % 4,
        encryption=_WPA2 if index % 2 else _OPEN,
        wps=bool(index % 3),
        wps_locked=bool(index % 6 == 0),
        identity="Netgear" if index % 2 else "",
    )
    defaults.update(overrides)
    return APRow(**defaults)


def _rows(count: int) -> list[APRow]:
    return [_row(i) for i in range(count)]


pytestmark = pytest.mark.asyncio(loop_scope="module")


class _TableHost(App):
    CSS = "APTable { height: 1fr; }"

    def compose(self) -> ComposeResult:
        yield APTable(id="t")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def _table_app():
    app = _TableHost()
    async with app.run_test(size=(100, 16)) as pilot:
        await pilot.pause(0)
        yield app.query_one("#t", APTable), pilot


@pytest_asyncio.fixture(loop_scope="module")
async def table(_table_app):
    """The shared table, wound back to a known state."""
    table, pilot = _table_app
    table.app.theme = "textual-dark"
    table.set_rows([])
    table.sort_column = "signal"
    table.sort_reverse = True
    table.scroll_to(x=0, y=0, animate=False)
    table.focus()
    await pilot.pause(0)
    yield table, pilot


def _snapshot(table: APTable) -> list:
    return [table.render_line(y) for y in range(table.size.height)]


def _assert_render_is_honest(table: APTable) -> None:
    """The cached render must be byte-identical to a render with every cache dropped."""
    warm = _snapshot(table)
    table.invalidate()
    assert _snapshot(table) == warm


# ----- Cache honesty ---------------------------------------------------------


async def test_render_stays_honest_across_a_mutation_script(table):
    table, pilot = table
    rows = _rows(40)
    table.set_rows(rows)
    _assert_render_is_honest(table)

    # A single cell moves.
    rows[3] = replace(rows[3], beacons=rows[3].beacons + 1)
    table.set_rows(rows)
    _assert_render_is_honest(table)

    # A row goes stale (dims every cell in it).
    rows[4] = replace(rows[4], is_stale=True)
    table.set_rows(rows)
    _assert_render_is_honest(table)

    # The cursor moves.
    table.move_cursor(6)
    await pilot.pause(0)
    _assert_render_is_honest(table)

    # The sort column changes, re-ordering rows under a stationary cursor.
    table.sort_column = "ssid"
    await pilot.pause(0)
    _assert_render_is_honest(table)

    table.sort_reverse = not table.sort_reverse
    await pilot.pause(0)
    _assert_render_is_honest(table)

    # A column gets wider.
    rows[5] = replace(rows[5], ssid="A Very Considerably Longer Name")
    table.set_rows(rows)
    _assert_render_is_honest(table)

    # …and narrower again.
    rows[5] = replace(rows[5], ssid="x")
    table.set_rows(rows)
    _assert_render_is_honest(table)

    # Rows leave and arrive.
    table.set_rows(rows[:20])
    _assert_render_is_honest(table)
    table.set_rows(rows[:20] + [_row(99, ssid="Latecomer")])
    _assert_render_is_honest(table)

    # The viewport scrolls.
    table.scroll_to(y=8, animate=False)
    await pilot.pause(0)
    _assert_render_is_honest(table)

    # A badge appears, which changes the SSID cell without changing the SSID.
    rows[5] = replace(rows[5], has_wps_psk=True)
    table.set_rows(rows[:20])
    _assert_render_is_honest(table)


async def test_only_the_changed_cell_is_re_rendered(table):
    table, pilot = table
    rows = _rows(40)
    table.set_rows(rows)
    _snapshot(table)                      # warm every visible row

    calls = []
    original = table.app.console.render_lines
    table.app.console.render_lines = lambda *a, **k: (calls.append(a[0]), original(*a, **k))[1]
    try:
        rows[2] = replace(rows[2], beacons=rows[2].beacons + 1)
        table.set_rows(rows)
        assert calls == []                # set_rows never touches the console
        table.render_line(table._index[rows[2].bssid] + 1)
        assert len(calls) == 1            # exactly the 🥓 cell
    finally:
        table.app.console.render_lines = original


async def test_unchanged_rows_repaint_nothing(table):
    table, pilot = table
    rows = _rows(40)
    table.set_rows(rows)

    refreshed = []
    table.refresh_line = lambda y: refreshed.append(y)
    table.set_rows(list(rows))
    assert refreshed == []

    rows[9] = replace(rows[9], signal=-91)
    table.set_rows(rows)
    assert refreshed == [table._index[rows[9].bssid] + 1]


# ----- Sorting ---------------------------------------------------------------


@pytest.mark.parametrize("column", [c.key for c in COLUMNS])
async def test_every_column_sorts_both_ways_without_error(table, column):
    table, pilot = table
    table.set_rows(_rows(20))
    for reverse in (True, False):
        table.sort_column = column
        table.sort_reverse = reverse
        table.resort()
        assert len(table.ordered_bssids) == 20
    _assert_render_is_honest(table)


# ----- Scanner wiring --------------------------------------------------------
#
# APTable's messages reach ScannerView by handler name, and camel_to_snake("APTable")
# is "aptable" — so the messages declare namespace="ap_table" to keep the readable
# on_ap_table_* names. Get that wrong and enter/click silently do nothing.


async def test_scanner_handles_every_ap_table_message():
    for message in (APTable.RowSelected, APTable.SortChanged):
        assert hasattr(ScannerView, message.handler_name), message.handler_name


class _FocusStub(Screen):
    pass


class _ScannerHost(App):
    def __init__(self, aps):
        super().__init__()
        self.focus_view = _FocusStub()
        self.array = _StubArray(aps)
        self.pbc_enabled = False
        self.vault = Vault()
        self.target_ap = None

    def persist_config(self) -> None:
        pass

    def on_mount(self) -> None:
        self.push_screen(ScannerView())


class _StubArray:
    def __init__(self, aps):
        self.access_points = {ap.bssid: ap for ap in aps}
        self.clients = {}
        self.forged_macs = set()
        self.supported_channels = [1, 6, 11]
        self.members = []

    def get_access_points(self, include_eviltwin=True):
        return list(self.access_points.values())

    async def start_hopping(self, channels=None, interval=0.25, include_dfs_channels=True):
        pass

    async def stop_hopping(self):
        pass


def _scanner_aps():
    aps = []
    for index, signal in enumerate((-40, -50, -60)):
        ap = AccessPoint(bssid=f"02:00:00:00:00:{index:02x}", ssid=f"Net{index}", channel=1)
        ap.signal_by_card = {"card0": signal}
        aps.append(ap)
    return aps


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def _scanner_app():
    aps = _scanner_aps()
    app = _ScannerHost(aps)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause(0)
        # These tests drive refresh_table() themselves; leaving the 15 Hz tick running
        # just makes every pilot.pause() wait on another round of timer messages.
        app.screen._refresh_timer.stop()
        yield app, aps, pilot


@pytest_asyncio.fixture(loop_scope="module")
async def scanner_host(_scanner_app):
    """The shared ScannerView, back on top of the stack with a default sort."""
    app, aps, pilot = _scanner_app
    while not isinstance(app.screen, ScannerView):
        app.pop_screen()
    app.target_ap = None
    table = app.screen.query_one("#ap-table", APTable)
    table.sort_column = "signal"
    table.sort_reverse = True
    Config.scanner_sort = "signal"
    app.screen.refresh_table()
    await pilot.pause()
    yield app, aps, pilot


async def test_clicking_a_header_re_sorts_and_persists(scanner_host):
    app, aps, pilot = scanner_host
    table = app.screen.query_one("#ap-table", APTable)
    assert table.sort_column == "signal"

    await pilot.click(APTable, offset=(4, 0))     # the SSID header
    assert table.sort_column == "ssid"
    assert Config.scanner_sort == "ssid"
    assert isinstance(app.screen, ScannerView)    # a header click must not select a row


@pytest.mark.parametrize("reverse", [False, True])
async def test_chipset_sort_keeps_blank_rows_last(reverse):
    column = COLUMNS[COLUMN_KEYS.index("chipset")]
    rows = [_row(0, chipset=""), _row(1, chipset="Broadcom"), _row(2, chipset="MediaTek")]
    ordered = sorted(rows, key=lambda row: column.sort_key(row, reverse), reverse=reverse)
    assert ordered[-1].chipset == ""
