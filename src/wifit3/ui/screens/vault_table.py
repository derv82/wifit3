import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Input, Tabs, Tab
from textual.widget import Widget

from wifit3.models import CaptureType, PersistedCapture
from wifit3.persist.meta import meta_path, read_meta
from textual.message import Message


def _search_forms(value: str) -> tuple[str, str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    ascii_form = "".join(
        character
        for character in unicodedata.normalize("NFKD", normalized)
        if not unicodedata.combining(character) and character.isascii()
    )
    return normalized, ascii_form


def matches_search(query: str, fields: Iterable[str]) -> bool:
    """True when ``query`` is in any field, ignoring case, width and accents."""
    query_form, ascii_query = _search_forms(query)
    for field in fields:
        normalized, ascii_form = _search_forms(field)
        if query_form in normalized or ascii_query and ascii_query in ascii_form:
            return True
    return False


class VaultSearchBar(Horizontal):
    """The Vault's search input; Esc and Enter return focus to the AP list."""

    BINDINGS = [Binding("escape", "leave", "", show=False)]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="SSID, BSSID, or filename", id="vault-search", compact=True)

    def action_leave(self) -> None:
        self.screen.query_one("#vault-aps", DataTable).focus()

    def on_input_submitted(self) -> None:
        self.action_leave()


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
    VaultTable #vault-search-bar {
        height: auto;
        padding: 0 1;
        border: round $primary;
        border-title-color: $primary;
        border-title-style: bold;
    }
    VaultTable #vault-search {
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
        self._nickname_revision = -1
        self._current_tab = "tab-all"
        self._search_query = ""
        self._is_reloading = False

    def compose(self) -> ComposeResult:
        search_bar = VaultSearchBar(id="vault-search-bar")
        search_bar.border_title = "SEARCH"
        yield search_bar
        yield Tabs(id="vault-tabs")
        yield DataTable(id="vault-aps", show_header=False, cursor_type="row")

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
        if self._is_reloading:
            return
        self._is_reloading = True
        try:
            self._aps = self._group_aps()
            if self._nickname_revision != self.app.vault.revision:
                self._nicknames = {}
                for bssid, (ssid, _) in self._aps.items():
                    nickname = read_meta(meta_path(ssid, bssid)).get("nickname")
                    if isinstance(nickname, str) and nickname:
                        self._nicknames[bssid] = nickname
                self._nickname_revision = self.app.vault.revision
            table = self.query_one("#vault-aps", DataTable)
            selected_bssid = self._current_row_key(table)
            table.clear()
            
            # Determine available tabs
            has_psk = False
            has_wep = False
            has_pin = False
            has_hs = False
            has_pmk = False
            
            for _, caps in self._aps.values():
                if any(c.type in (CaptureType.WPS_PIN, CaptureType.WPS_PBC, CaptureType.WPA_PSK) and c.value for c in caps):
                    has_psk = True
                if any(c.type == CaptureType.WEP for c in caps):
                    has_wep = True
                if any(c.type == CaptureType.WPS_PIN and c.pin is not None for c in caps):
                    has_pin = True
                if any(c.type == CaptureType.HS for c in caps):
                    has_hs = True
                if any(c.type == CaptureType.PMKID for c in caps):
                    has_pmk = True
                    
            # Manage Tabs
            tabs = self.query_one("#vault-tabs", Tabs)
            
            def _ensure_tab(tab_id: str, title: str, exists: bool):
                tab = tabs.get_tab(tab_id)
                if tab is not None:
                    if not exists:
                        tabs.remove_tab(tab_id)
                else:
                    if exists:
                        tabs.add_tab(Tab(title, id=tab_id))
                        
            _ensure_tab("tab-all", "ALL", True)
            _ensure_tab("tab-psk", "PSK", has_psk)
            _ensure_tab("tab-wep", "WEP KEY", has_wep)
            _ensure_tab("tab-pin", "WPS PIN", has_pin)
            _ensure_tab("tab-hs", "HANDSHAKE", has_hs)
            _ensure_tab("tab-pmk", "PMKID", has_pmk)
            
            try:
                tabs.active = self._current_tab
            except Exception:
                self._current_tab = "tab-all"
                tabs.active = "tab-all"

            # Sort Alphabetical by SSID
            ssid_counts: Dict[str, int] = {}
            for ssid, _ in self._aps.values():
                ssid_counts[ssid or ""] = ssid_counts.get(ssid or "", 0) + 1
                
            rows = sorted(self._aps.items(), key=lambda kv: ((kv[1][0] or "").lower(), kv[0]))
            
            # Populate current tab
            for bssid, (ssid, caps) in rows:
                if self._search_query and not matches_search(
                        self._search_query,
                        [ssid or "", bssid, self._nicknames.get(bssid, ""),
                         *(Path(c.path).name for c in caps)]):
                    continue
                # Check filter
                if self._current_tab == "tab-psk" and not any(c.type in (CaptureType.WPS_PIN, CaptureType.WPS_PBC, CaptureType.WPA_PSK) and c.value for c in caps): continue
                if self._current_tab == "tab-wep" and not any(c.type == CaptureType.WEP for c in caps): continue
                if self._current_tab == "tab-pin" and not any(c.type == CaptureType.WPS_PIN and c.pin is not None for c in caps): continue
                if self._current_tab == "tab-hs" and not any(c.type == CaptureType.HS for c in caps): continue
                if self._current_tab == "tab-pmk" and not any(c.type == CaptureType.PMKID for c in caps): continue
                
                # Build Row Markup
                name = escape(self.display_name(bssid, ssid))
                bssid_suffix = f" [dim]{bssid}[/dim]" if ssid_counts.get(ssid or "", 0) > 1 else ""
                
                badges = []
                if any(c.type in (CaptureType.WPS_PIN, CaptureType.WPS_PBC, CaptureType.WPA_PSK) and c.value for c in caps):
                    badges.append("[bold green]✓PSK[/]")
                if any(c.type == CaptureType.WPS_PIN and c.pin is not None for c in caps):
                    badges.append("[bold green]✓PIN[/]")
                if any(c.type == CaptureType.WEP for c in caps):
                    badges.append("[bold green]✓WEP[/]")
                if any(c.type == CaptureType.HS for c in caps):
                    badges.append("[bold cyan]✓HS[/]")
                if any(c.type == CaptureType.PMKID for c in caps):
                    badges.append("[bold cyan]✓PMK[/]")
                
                badges_str = " ".join(badges)
                markup = f"{name} {badges_str}{bssid_suffix}".strip()
                table.add_row(Text.from_markup(markup, emoji=False), key=bssid)

            # Restore the prior selection when that AP survived the reload.
            if selected_bssid is not None:
                try:
                    table.move_cursor(row=table.get_row_index(selected_bssid))
                except Exception:
                    pass

            self.border_title = f"VAULT ({len(self._aps)} APs)"
            self.post_message(self.TableReloaded())
        finally:
            self._is_reloading = False

    def display_name(self, bssid: str, ssid: Optional[str]) -> str:
        """``nickname (SSID)`` when the AP has a nickname, else the SSID."""
        name = ssid or "‹hidden›"
        nickname = self._nicknames.get(bssid)
        return f"{nickname} ({name})" if nickname else name

    def reload_nicknames(self) -> None:
        """Re-read every nickname from its .meta, then reload the rows."""
        self._nickname_revision = -1
        self.reload_table()

    def _current_row_key(self, table: DataTable) -> Optional[str]:
        """The bssid under the cursor, so a reload can restore the selection."""
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    @on(Input.Changed, "#vault-search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        if query != self._search_query:
            self._search_query = query
            self.reload_table()

    @on(Tabs.TabActivated, "#vault-tabs")
    def _on_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab and event.tab.id:
            if self._current_tab != event.tab.id:
                self._current_tab = event.tab.id
                self.reload_table()
