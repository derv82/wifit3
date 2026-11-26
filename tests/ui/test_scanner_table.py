"""APTable: what a row change re-renders, and the chipset sort."""
from dataclasses import replace

import pytest
import pytest_asyncio
from textual.app import App, ComposeResult

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


# ----- Cache honesty ---------------------------------------------------------


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


# ----- Scanner wiring --------------------------------------------------------
#
# APTable's messages reach ScannerView by handler name, and camel_to_snake("APTable")
# is "aptable" — so the messages declare namespace="ap_table" to keep the readable
# on_ap_table_* names. Get that wrong and enter/click silently do nothing.


async def test_scanner_handles_every_ap_table_message():
    for message in (APTable.RowSelected, APTable.SortChanged):
        assert hasattr(ScannerView, message.handler_name), message.handler_name


@pytest.mark.parametrize("reverse", [False, True])
async def test_chipset_sort_keeps_blank_rows_last(reverse):
    column = COLUMNS[COLUMN_KEYS.index("chipset")]
    rows = [_row(0, chipset=""), _row(1, chipset="Broadcom"), _row(2, chipset="MediaTek")]
    ordered = sorted(rows, key=lambda row: column.sort_key(row, reverse), reverse=reverse)
    assert ordered[-1].chipset == ""
