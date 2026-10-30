import unicodedata
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Input, Tabs, Tab
from textual.widget import Widget

from wifit3.models import PSK_CAPTURE_TYPES, CaptureType, PersistedCapture
from wifit3.persist.ap_state import PasswordStatus
from wifit3.persist.meta import meta_path, read_meta
from wifit3.ui.colors import PASSWORD_MISMATCH_COLOR
from textual.message import Message


def _filter_forms(value: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    ascii_form = "".join(
        character
        for character in unicodedata.normalize("NFKD", normalized)
        if not unicodedata.combining(character) and character.isascii()
    )
    return normalized, ascii_form


def matches_filter(query: str, fields: Iterable[str]) -> bool:
    """True when ``query`` is in any field, ignoring case, width and accents."""
    query_form, ascii_query = _filter_forms(query)
    for field in fields:
        normalized, ascii_form = _filter_forms(field)
        if query_form in normalized or ascii_query and ascii_query in ascii_form:
            return True
    return False


def _has_psk(captures: List[PersistedCapture]) -> bool:
    return any(c.type in PSK_CAPTURE_TYPES and c.value for c in captures)


def _has_wep_key(captures: List[PersistedCapture]) -> bool:
    return any(c.type == CaptureType.WEP for c in captures)


def _has_wps_pin(captures: List[PersistedCapture]) -> bool:
    return any(c.type == CaptureType.WPS_PIN and c.pin is not None for c in captures)


def _has_handshake(captures: List[PersistedCapture]) -> bool:
    return any(c.type == CaptureType.HS for c in captures)


def _has_pmkid(captures: List[PersistedCapture]) -> bool:
    return any(c.type == CaptureType.PMKID for c in captures)


_TAB_TITLES: Dict[str, str] = {
    "tab-all": "ALL", "tab-psk": "PSK", "tab-wep": "WEP KEY", "tab-pin": "WPS PIN", "tab-hs": "HANDSHAKE",
    "tab-pmk": "PMKID",
}

_TAB_FILTERS: Dict[str, Callable[[List[PersistedCapture]], bool]] = {
    "tab-psk": _has_psk, "tab-wep": _has_wep_key, "tab-pin": _has_wps_pin, "tab-hs": _has_handshake,
    "tab-pmk": _has_pmkid,
}


class VaultFilterBar(Horizontal):
    """The Vault's filter input; Esc and Enter return focus to the AP list."""

    BINDINGS = [Binding("escape", "leave", "", show=False)]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="SSID, BSSID, or filename", id="vault-filter", compact=True)

    def action_leave(self) -> None:
        self.screen.query_one("#vault-aps", DataTable).focus()

    def on_input_submitted(self) -> None:
        self.action_leave()


class VaultApsTable(DataTable):
    BINDINGS = [
        Binding("k", "cursor_up", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("home,g", "scroll_top", show=False),
        Binding("end,G", "scroll_bottom", show=False),
    ]


class VaultTable(Widget):
    """The ESSID Table for the Vault drawer."""

    class TableReloaded(Message):
        """Emitted when the table finishes reloading its rows."""

    DEFAULT_CSS = """
    VaultTable {
        width: 48;
        height: 1fr;
        border: heavy $primary;
        border-title-color: $primary;
        border-title-style: bold;
    }
    VaultTable #vault-filter-bar {
        height: auto;
        padding: 0 1;
        border: round $primary;
        border-title-color: $primary;
        border-title-style: bold;
    }
    VaultTable #vault-filter {
        width: 1fr;
    }
    VaultTable Tabs {
        margin-bottom: 0;
    }
    VaultTable DataTable {
        height: 1fr;
    }
    """

    def __init__(self, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self._aps: Dict[str, Tuple[Optional[str], List[PersistedCapture]]] = {}
        self._nicknames: Dict[str, str] = {}
        self._nicknames_stale = True
        self._current_tab = "tab-all"
        self._filter_text = ""
        self._is_reloading = False

    def compose(self) -> ComposeResult:
        filter_bar = VaultFilterBar(id="vault-filter-bar")
        filter_bar.border_title = "[u]F[/u]ILTER"
        yield filter_bar
        yield Tabs(id="vault-tabs")
        yield VaultApsTable(id="vault-aps", show_header=False, cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#vault-aps", DataTable)
        table.add_column("AP", key="ap")
        self.reload_table()

    def _group_aps(self) -> Dict[str, Tuple[Optional[str], List[PersistedCapture]]]:
        captures_by_bssid: Dict[str, List[PersistedCapture]] = {}
        for cap in self.app.vault.all_captures():
            captures_by_bssid.setdefault(cap.bssid, []).append(cap)
        return {bssid: (self.app.vault.ssid(bssid), caps)
                for bssid, caps in captures_by_bssid.items()}

    def reload_table(self) -> None:
        if self._is_reloading or not self.is_attached:
            return
        self._is_reloading = True
        try:
            self._aps = self._group_aps()
            if self._nicknames_stale:
                self._nicknames = {}
                for bssid, (ssid, _) in self._aps.items():
                    nickname = read_meta(meta_path(ssid, bssid)).get("nickname")
                    if isinstance(nickname, str) and nickname:
                        self._nicknames[bssid] = nickname
                self._nicknames_stale = False
            self._update_tabs()
            table = self.query_one("#vault-aps", DataTable)
            shown = self._shown_bssids()
            ssid_counts = self._ssid_counts()
            if [row.key.value for row in table.ordered_rows] == shown:
                for bssid in shown:
                    text = self._row_text(bssid, ssid_counts)
                    if table.get_cell(bssid, "ap") != text:
                        table.update_cell(bssid, "ap", text, update_width=True)
            else:
                self._replace_rows(table, shown, ssid_counts)
            self.border_title = f"VAULT ({len(self._aps)} APs)"
            self.post_message(self.TableReloaded())
        finally:
            self._is_reloading = False

    def _replace_rows(self, table: DataTable, shown: List[str], ssid_counts: Dict[str, int]) -> None:
        """Rebuild the rows as ``shown``, keeping the cursor's AP (else its row) at the same height."""
        selected_bssid = self._current_row_key(table)
        cursor_row, scroll_y = table.cursor_row, table.scroll_y
        with table.prevent(DataTable.RowHighlighted):
            table.clear()
            for bssid in shown:
                table.add_row(self._row_text(bssid, ssid_counts), key=bssid)
            if table.row_count == 0:
                return
            if selected_bssid in table.rows:
                row = table.get_row_index(selected_bssid)
            else:
                row = min(cursor_row, table.row_count - 1)
            table.move_cursor(row=row, scroll=False)
        table.scroll_to(y=scroll_y + row - cursor_row, animate=False, immediate=True)

    def _update_tabs(self) -> None:
        """A tab for each kind of capture the vault holds; back to ALL when the current tab goes."""
        tabs = self.query_one("#vault-tabs", Tabs)
        for tab_id, title in _TAB_TITLES.items():
            belongs = _TAB_FILTERS.get(tab_id)
            exists = belongs is None or any(belongs(caps) for _, caps in self._aps.values())
            if tabs.get_tab(tab_id) is not None:
                if not exists:
                    tabs.remove_tab(tab_id)
            elif exists:
                tabs.add_tab(Tab(title, id=tab_id))
        try:
            tabs.active = self._current_tab
        except Exception:
            self._current_tab = "tab-all"
            tabs.active = "tab-all"

    def _ssid_counts(self) -> Dict[str, int]:
        ssid_counts: Dict[str, int] = {}
        for ssid, _ in self._aps.values():
            ssid_counts[ssid or ""] = ssid_counts.get(ssid or "", 0) + 1
        return ssid_counts

    def _shown_bssids(self) -> List[str]:
        """The APs passing the filter and the current tab, sorted by SSID."""
        belongs = _TAB_FILTERS.get(self._current_tab)
        shown = []
        for bssid, (ssid, caps) in sorted(self._aps.items(), key=lambda kv: ((kv[1][0] or "").lower(), kv[0])):
            if self._filter_text and not matches_filter(
                    self._filter_text,
                    [ssid or "", bssid, self._nicknames.get(bssid, ""), *(Path(c.path).name for c in caps)]):
                continue
            if belongs is None or belongs(caps):
                shown.append(bssid)
        return shown

    def _row_text(self, bssid: str, ssid_counts: Dict[str, int]) -> Text:
        ssid, caps = self._aps[bssid]
        name = escape(self.display_name(bssid, ssid))
        bssid_suffix = f" [dim]{bssid}[/dim]" if ssid_counts.get(ssid or "", 0) > 1 else ""
        badges = []
        if _has_psk(caps):
            state = self.app.vault.cached_ap_state(bssid)
            stale = state is not None and state.status is PasswordStatus.STALE
            badges.append(f"[bold {PASSWORD_MISMATCH_COLOR if stale else 'green'}]✓PSK[/]")
        if _has_wps_pin(caps):
            badges.append("[bold green]✓PIN[/]")
        if _has_wep_key(caps):
            badges.append("[bold green]✓WEP[/]")
        markup = f"{name} {' '.join(badges)}{bssid_suffix}".strip()
        return Text.from_markup(markup, emoji=False)

    def display_name(self, bssid: str, ssid: Optional[str]) -> str:
        """``nickname (SSID)`` when the AP has a nickname, else the SSID."""
        name = ssid or "‹hidden›"
        nickname = self._nicknames.get(bssid)
        return f"{nickname} ({name})" if nickname else name

    def reload_nicknames(self) -> None:
        """Re-read every nickname from its .meta, then reload the rows."""
        self._nicknames_stale = True
        self.reload_table()

    def _current_row_key(self, table: DataTable) -> Optional[str]:
        """The bssid under the cursor, so a reload can restore the selection."""
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    @on(Input.Changed, "#vault-filter")
    def _on_filter_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        if query != self._filter_text:
            self._filter_text = query
            self.reload_table()

    @on(Tabs.TabActivated, "#vault-tabs")
    def _on_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab and event.tab.id:
            if self._current_tab != event.tab.id:
                self._current_tab = event.tab.id
                self.reload_table()
