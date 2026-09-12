"""_APScanTable scroll-suppression contract.

The scanner re-sorts on a timer while the user scrolls/navigates. pin_cursor_row
must keep the cursor (highlight) on the same row WITHOUT moving the viewport,
while the stock move_cursor still scrolls the cursor into view. These are the two
halves of the "don't snap the viewport on auto-sort, but don't let the selection
silently drift either" fix.
"""
import pytest
import pytest_asyncio
from textual.app import App, ComposeResult
from textual.widgets.data_table import ColumnKey

from wifit3.models import AccessPoint
from wifit3.ui.screens.scanner import ScannerView, _APScanTable


class _TableApp(App):
    # Constrain the table to the screen so 50 rows overflow and it scrolls
    # internally (otherwise DataTable sizes to content and never scrolls).
    CSS = "#t { height: 100%; }"

    def compose(self) -> ComposeResult:
        yield _APScanTable(cursor_type="row", id="t")

    def on_mount(self) -> None:
        table = self.query_one("#t", _APScanTable)
        table.add_column("v", key="v")
        for i in range(50):
            table.add_row(str(i), key=f"r{i}")


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def table_host():
    app = _TableApp()
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.pause(0)
        yield app.query_one("#t", _APScanTable), pilot


async def _reset(table, pilot):
    table._suppress_scroll = False
    table.move_cursor(row=0, animate=False)
    await pilot.pause(0)


@pytest.mark.asyncio(loop_scope="module")
async def test_pin_cursor_row_moves_highlight_without_scrolling(table_host):
    table, pilot = table_host
    await _reset(table, pilot)
    assert table.scroll_offset.y == 0
    assert table.cursor_coordinate.row == 0

    # Pin far off-screen: the highlight follows the row, viewport stays put.
    table.pin_cursor_row(40)
    await pilot.pause(0)
    assert table.cursor_coordinate.row == 40, "highlight must track the row"
    assert table.scroll_offset.y == 0, "pin must not move the viewport"


@pytest.mark.asyncio(loop_scope="module")
async def test_move_cursor_still_scrolls_into_view(table_host):
    # Contrast: the stock path (used by explicit user sorts) DOES recenter.
    table, pilot = table_host
    await _reset(table, pilot)
    table.move_cursor(row=40, animate=False)
    await pilot.pause(0)
    assert table.scroll_offset.y > 0, "move_cursor should scroll the cursor into view"


@pytest.mark.asyncio(loop_scope="module")
async def test_pin_releases_suppress_flag_after_refresh(table_host):
    # The flag must clear after the move so ordinary user navigation
    # (arrow keys past the viewport edge) scrolls normally again.
    table, pilot = table_host
    await _reset(table, pilot)
    table.pin_cursor_row(40)
    await pilot.pause()   # _release_scroll runs after a render (call_after_refresh); pause() waits for it
    assert table._suppress_scroll is False


class _SsidTableApp(App):
    def compose(self) -> ComposeResult:
        table = _APScanTable(id="ssid_table")
        table.add_column("SSID  ", key="ssid")
        table.add_column("CH  ", key="channel")
        yield table


@pytest.mark.asyncio
async def test_ap_scan_table_ssid_column_clamps_to_min_width():
    app = _SsidTableApp()
    async with app.run_test() as pilot:
        table = app.query_one("#ssid_table", _APScanTable)
        col = table.columns[ColumnKey("ssid")]
        assert col.content_width == _APScanTable.SSID_MIN_WIDTH

        table.add_row("Net1", "1", key="r1")
        await pilot.pause()
        assert col.content_width == _APScanTable.SSID_MIN_WIDTH

        table.update_cell("r1", "ssid", "A", update_width=True)
        await pilot.pause()
        assert col.content_width == _APScanTable.SSID_MIN_WIDTH


@pytest.mark.asyncio
async def test_ap_scan_table_ssid_column_expands_to_long_ssid():
    app = _SsidTableApp()
    async with app.run_test() as pilot:
        table = app.query_one("#ssid_table", _APScanTable)
        col = table.columns[ColumnKey("ssid")]

        long_ssid = "Super Long Test Access Point 30"
        table.add_row(long_ssid, "6", key="r2")
        await pilot.pause()
        assert col.content_width == len(long_ssid)


class _FakeDeviceManager:
    def __init__(self, aps):
        self.access_points = {ap.bssid: ap for ap in aps}
        self.clients = {}
        self.forged_macs = set()
        self.supported_channels = [1, 6, 11]
        self.members = []

    def get_access_points(self, include_eviltwin: bool = True):
        return list(self.access_points.values())

    async def start_hopping(self, *a, **k):
        pass

    async def stop_hopping(self):
        pass


class _ScannerHostApp(App):
    def __init__(self, array):
        super().__init__()
        self.array = array
        self.pbc_enabled = True
    def on_mount(self):
        self.push_screen(ScannerView())


@pytest.mark.asyncio
async def test_scanner_view_ssid_width_decloaks_and_caps():
    ap_hidden = AccessPoint(bssid="00:11:22:33:44:01", ssid=None)
    ap_hidden.signal_by_card = {"card0": -50}

    fake_mgr = _FakeDeviceManager([ap_hidden])
    app = _ScannerHostApp(fake_mgr)
    async with app.run_test() as pilot:
        await pilot.pause(0)
        scanner = app.screen
        assert isinstance(scanner, ScannerView)
        table = scanner.query_one("#ap-table", _APScanTable)
        col = table.columns[ColumnKey("ssid")]

        scanner.refresh_table()
        await pilot.pause()
        assert col.content_width == _APScanTable.SSID_MIN_WIDTH

        ap_hidden.ssid = "Super Long Test Access Point 30"
        scanner.refresh_table()
        await pilot.pause()
        assert col.content_width == len(ap_hidden.ssid)

        ap_huge = AccessPoint(bssid="00:11:22:33:44:02", ssid="A" * 50)
        ap_huge.signal_by_card = {"card0": -40}
        fake_mgr.access_points[ap_huge.bssid] = ap_huge
        scanner.refresh_table()
        await pilot.pause()
        assert col.content_width == ScannerView._SSID_CELL_MAX
