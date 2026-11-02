"""What the vault benchmarks share: open the vault from the splash, select an AP, and wait until the item
pane has composed an AP's state and the screen has painted it. Keys are posted, as Pilot.press waits for idle."""
import asyncio
import shutil
import time
from typing import Callable, Optional, TypeVar

from textual import events
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import DataTable, Label

from wifit3.persist.ap_state import ApState
from wifit3.ui.app import WifiteApp
from wifit3.ui.screens.vault_drawer import VaultDrawer
from wifit3.ui.screens.vault_item import VaultItemView, _ApStatePanel, _KeyRow

T = TypeVar("T")


def terminal_size() -> tuple[int, int]:
    columns, lines = shutil.get_terminal_size()
    return columns, lines


async def wait_for(condition: Callable[[], Optional[T]], timeout_s: float = 120.0) -> T:
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        try:
            if result := condition():
                return result
        except NoMatches:
            pass
        await asyncio.sleep(0.002)
    raise TimeoutError("the vault never got there")


async def painted(screen: Screen) -> float:
    """perf_counter once the screen has laid out and rendered everything pending."""
    done = asyncio.get_running_loop().create_future()
    screen.call_after_refresh(lambda: done.set_result(time.perf_counter()))
    return await done


def aps(drawer: VaultDrawer) -> DataTable:
    return drawer.query_one("#vault-aps", DataTable)


def bssid_at(table: DataTable, row: int) -> str:
    return table.coordinate_to_cell_key((row, 0)).row_key.value


def bssid_of(app: WifiteApp, ssid: str) -> str:
    return next(c.bssid for c in app.vault.all_captures() if c.ssid == ssid)


def shown_state(drawer: VaultDrawer, bssid: str) -> Optional[ApState]:
    """The state the item pane has composed and mounted, when it is ``bssid``'s; None while it is
    another AP's or an update is under way."""
    if drawer._pending_bssid is not None:
        return None
    item = drawer.query_one("#vault-item", VaultItemView)
    panel = next(iter(item.query_children(_ApStatePanel).results(_ApStatePanel)), None)
    if (item.lock.is_locked or panel is None or panel._state is None or item._bssid != bssid
            or panel._state.bssid != bssid):
        return None
    state = panel._state
    uncracked = panel._uncracked_list
    if (uncracked._exhausted is not state.exhausted_wordlists) if uncracked is not None else state.uncracked:
        return None
    rows = list(panel.query(_KeyRow).results(_KeyRow))
    if len(rows) != len(state.keys) or any(row._state is not state for row in rows):
        return None
    if {str(label.content) for label in panel.query(".ssid").results(Label)} != (
            {history.ssid for history in state.ssids} or {"‹hidden›"}):
        return None
    if any(row.expanded and row._has_history and not row.history_rows.children for row in rows):
        return None
    return state if all(widget.is_mounted for widget in panel.walk_children()) else None


async def open_vault(app: WifiteApp) -> tuple[VaultDrawer, float]:
    """Post v; the drawer once it is open with its first AP painted, and when that was."""
    app.post_message(events.Key("v", "v"))
    drawer = await wait_for(lambda: app.screen if isinstance(app.screen, VaultDrawer) else None)
    await wait_for(lambda: drawer.has_class("open") and aps(drawer).row_count
                   and shown_state(drawer, bssid_at(aps(drawer), aps(drawer).cursor_row)))
    return drawer, await painted(drawer)


async def select(drawer: VaultDrawer, bssid: str) -> None:
    """Move the AP list's cursor to ``bssid``; returns once its state is painted."""
    aps(drawer).move_cursor(row=aps(drawer).get_row_index(bssid))
    await wait_for(lambda: shown_state(drawer, bssid))
    await painted(drawer)
