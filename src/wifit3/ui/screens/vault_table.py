from typing import Dict, List, Optional, Tuple

from textual import on
from textual.app import ComposeResult
from textual.widgets import DataTable, Tabs, Tab
from textual.widget import Widget

from wifit3.models import CaptureType, PersistedCapture
from textual.message import Message

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
    VaultTable Tabs {
        margin-bottom: 1;
    }
    VaultTable DataTable {
        height: 1fr;
    }
    """

    def __init__(self, id: Optional[str] = None) -> None:
        super().__init__(id=id)
        self._aps: Dict[str, Tuple[Optional[str], List[PersistedCapture]]] = {}
        self._current_tab = "tab-all"
        self._is_reloading = False

    def compose(self) -> ComposeResult:
        yield Tabs(id="vault-tabs")
        yield DataTable(id="vault-aps", show_header=False, cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#vault-aps", DataTable)
        table.add_column("AP", key="ap")
        self.reload_table()

    def _group_aps(self) -> Dict[str, Tuple[Optional[str], List[PersistedCapture]]]:
        groups: Dict[str, Tuple[Optional[str], List[PersistedCapture]]] = {}
        for cap in self.app.vault.all_captures():
            ssid, caps = groups.setdefault(cap.bssid, (None, []))
            caps.append(cap)
            if ssid is None and cap.ssid:
                groups[cap.bssid] = (cap.ssid, caps)
        return groups

    def reload_table(self) -> None:
        if self._is_reloading:
            return
        self._is_reloading = True
        try:
            self._aps = self._group_aps()
            table = self.query_one("#vault-aps", DataTable)
            table.clear()
            
            # Determine available tabs
            has_psk = False
            has_wep = False
            has_pin = False
            has_hs = False
            has_pmk = False
            
            for _, caps in self._aps.values():
                if any(c.type in (CaptureType.WPS_PIN, CaptureType.WPS_PBC) and c.value for c in caps):
                    has_psk = True
                if any(c.type == CaptureType.WEP for c in caps):
                    has_wep = True
                if any(c.type == CaptureType.WPS_PIN and c.pin for c in caps):
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
                # Check filter
                if self._current_tab == "tab-psk" and not any(c.type in (CaptureType.WPS_PIN, CaptureType.WPS_PBC) and c.value for c in caps): continue
                if self._current_tab == "tab-wep" and not any(c.type == CaptureType.WEP for c in caps): continue
                if self._current_tab == "tab-pin" and not any(c.type == CaptureType.WPS_PIN and c.pin for c in caps): continue
                if self._current_tab == "tab-hs" and not any(c.type == CaptureType.HS for c in caps): continue
                if self._current_tab == "tab-pmk" and not any(c.type == CaptureType.PMKID for c in caps): continue
                
                # Build Row Markup
                name = ssid or "‹hidden›"
                bssid_suffix = f" [dim]{bssid}[/dim]" if ssid_counts.get(ssid or "", 0) > 1 else ""
                
                badges = []
                if any(c.type in (CaptureType.WPS_PIN, CaptureType.WPS_PBC) and c.value for c in caps):
                    badges.append("[bold green]✓PSK[/]")
                if any(c.type == CaptureType.WPS_PIN and c.pin for c in caps):
                    badges.append("[bold green]✓PIN[/]")
                if any(c.type == CaptureType.WEP for c in caps):
                    badges.append("[bold green]✓WEP[/]")
                if any(c.type == CaptureType.HS for c in caps):
                    badges.append("[bold cyan]✓HS[/]")
                if any(c.type == CaptureType.PMKID for c in caps):
                    badges.append("[bold cyan]✓PMK[/]")
                
                badges_str = " ".join(badges)
                markup = f"{name} {badges_str}{bssid_suffix}".strip()
                table.add_row(markup, key=bssid)

            self.border_title = f"VAULT ({len(self._aps)} APs)"
            self.post_message(self.TableReloaded())
        finally:
            self._is_reloading = False

    @on(Tabs.TabActivated, "#vault-tabs")
    def _on_tab_activated(self, event: Tabs.TabActivated) -> None:
        if event.tab and event.tab.id:
            if self._current_tab != event.tab.id:
                self._current_tab = event.tab.id
                self.reload_table()
