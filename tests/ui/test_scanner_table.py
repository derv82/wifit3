"""_APScanTable scroll-suppression contract.

The scanner re-sorts on a timer while the user scrolls/navigates. pin_cursor_row
must keep the cursor (highlight) on the same row WITHOUT moving the viewport,
while the stock move_cursor still scrolls the cursor into view. These are the two
halves of the "don't snap the viewport on auto-sort, but don't let the selection
silently drift either" fix.

One 50-row table is booted once; each test resets it to the top first (the cost here
is the boot, and these are pure method calls, no clicks).
"""
import pytest
import pytest_asyncio
from textual.app import App, ComposeResult

from wifit3.ui.screens.scanner import _APScanTable


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
    """Return the shared table to its just-mounted state: top of the list, row 0, no suppression."""
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
    await pilot.pause()   # _release_scroll fires via call_after_refresh: must span a render, so idle-wait, not pause(0)
    assert table._suppress_scroll is False
