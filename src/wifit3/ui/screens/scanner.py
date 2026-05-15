from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, DataTable, RichLog
from textual.containers import Vertical, Horizontal
from textual.binding import Binding
from rich.markup import escape
from rich.text import Text

from wifit3.engine.models import AccessPoint
from typing import Dict

class ScannerView(Screen):
    """The main AP scanning list screen."""

    BINDINGS = [
        Binding("q", "app.quit", "Quit", show=True),
        Binding("c", "change_channel", "Channel Filter", show=True),
        Binding("s", "cycle_sort", "Sort Col", show=True),
        Binding("o", "toggle_sort_dir", "Sort Asc/Desc", show=True),
        Binding("l", "toggle_log", "Toggle Log", show=True),
        Binding("home", "scroll_home", "Top", show=False, priority=True),
        Binding("end", "scroll_end", "Bottom", show=False, priority=True)
    ]

    _COLUMNS = [
        ("bssid", "BSSID"),
        ("channel", "CH"),
        ("signal", "PWR"),
        ("encryption", "ENC"),
        ("ssid", "SSID"),
        ("beacons", "Beacons/sec")
    ]

    def __init__(self):
        super().__init__()
        self.ap_cache: Dict[str, AccessPoint] = {}
        self._refresh_timer = None
        self._sort_idx = 2 # Default to "signal" (PWR)
        self._sort_reverse = True

    def compose(self) -> ComposeResult:
        # A simple header with gradient-like styling (CSS handles the look)
        yield Header(show_clock=True)
        
        with Vertical():
            table = DataTable(cursor_type="row", id="ap-table")
            for key, label in self._COLUMNS:
                table.add_column(label, key=key)
            yield table
            
            # Collapsible Log
            yield RichLog(id="system-log", markup=True, highlight=True)
            
        yield Footer()

    async def on_mount(self) -> None:
        log = self.query_one("#system-log", RichLog)
        log.write("[bold green]Scanner Initialized.[/bold green]")
        self._update_column_headers()
        
        # We assume self.app.active_interface is set by SplashView before pushing this screen
        if self.app.active_interface:
            log.write(f"[cyan]Starting channel hopper on {self.app.active_interface.name}...[/cyan]")
            await self.app.active_interface.start_hopping(interval=0.25)
            # Start the 60FPS UI polling loop
            self._refresh_timer = self.set_interval(1 / 60, self.refresh_table)

    def _update_column_headers(self):
        table = self.query_one("#ap-table", DataTable)
        sort_key, _ = self._COLUMNS[self._sort_idx]
        indicator = " ▼" if self._sort_reverse else " ▲"
        
        for key, base_label in self._COLUMNS:
            label = base_label + indicator if key == sort_key else base_label
            # In Textual, updating column labels requires accessing columns dict
            if key in table.columns:
                table.columns[key].label = label
        table.refresh()

    def refresh_table(self) -> None:
        import time
        if not self.app.active_interface:
            return
            
        table = self.query_one("#ap-table", DataTable)
        
        # 60 FPS safe in-place updates
        for ap in self.app.active_interface.get_access_points():
            enc_display = ap.encryption
            if ap.wpa3:
                enc_display = "WPA3 (Trans)" if ap.transition_mode else "WPA3"
            
            # Calculate beacon rate
            elapsed = time.time() - ap.first_seen
            if elapsed < 1.0: elapsed = 1.0
            rate = ap.beacons / elapsed
            beacons_str = f"{ap.beacons} ({rate:.1f}/s)"
                
            if ap.bssid not in self.ap_cache:
                self.ap_cache[ap.bssid] = ap
                table.add_row(
                    Text(ap.bssid),
                    str(ap.channel),
                    f"{ap.signal}dBm",
                    enc_display,
                    Text(ap.ssid or "<Hidden>"),
                    beacons_str,
                    key=ap.bssid
                )
            else:
                # Check for decloak event
                old_ssid = self.ap_cache[ap.bssid].ssid
                if not old_ssid and ap.ssid:
                    log = self.query_one("#system-log", RichLog)
                    msg = Text.from_markup(f"[bold yellow][*] Decloaked Hidden Network: {escape(ap.bssid)} -> {escape(ap.ssid)}[/bold yellow]", emoji=False)
                    log.write(msg)
                
                self.ap_cache[ap.bssid] = ap
                table.update_cell(ap.bssid, "channel", str(ap.channel))
                table.update_cell(ap.bssid, "signal", f"{ap.signal}dBm")
                table.update_cell(ap.bssid, "encryption", enc_display)
                table.update_cell(ap.bssid, "ssid", Text(ap.ssid or "<Hidden>"))
                table.update_cell(ap.bssid, "beacons", beacons_str)
                
        self._apply_sort()

    def _apply_sort(self):
        table = self.query_one("#ap-table", DataTable)
        if table.row_count == 0:
            return
            
        # Track current cursor to prevent bouncing
        try:
            current_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            current_key = None

        sort_key, _ = self._COLUMNS[self._sort_idx]
        
        def safe_sort(val):
            if isinstance(val, str):
                if val.endswith("dBm"): return int(val[:-3])
                # For beacons like "100 (5.0/s)"
                if "(" in val:
                    parts = val.split()
                    if parts[0].isdigit(): return int(parts[0])
                if val.isdigit(): return int(val)
                if val.startswith("-") and val[1:].isdigit(): return int(val)
            return str(val).lower()
            
        table.sort(sort_key, key=safe_sort, reverse=self._sort_reverse)
        
        # Restore cursor
        if current_key:
            try:
                new_idx = table.get_row_index(current_key)
                table.move_cursor(row=new_idx, animate=False)
            except Exception:
                pass

    def action_toggle_log(self) -> None:
        log_widget = self.query_one("#system-log")
        log_widget.display = not log_widget.display

    def action_cycle_sort(self) -> None:
        self._sort_idx = (self._sort_idx + 1) % len(self._COLUMNS)
        self._update_column_headers()
        self._apply_sort()

    def action_toggle_sort_dir(self) -> None:
        self._sort_reverse = not self._sort_reverse
        self._update_column_headers()
        self._apply_sort()

    def action_scroll_home(self) -> None:
        table = self.query_one("#ap-table", DataTable)
        if table.row_count > 0:
            table.move_cursor(row=0, animate=True)
            
    def action_scroll_end(self) -> None:
        table = self.query_one("#ap-table", DataTable)
        if table.row_count > 0:
            table.move_cursor(row=table.row_count - 1, animate=True)

    def action_change_channel(self) -> None:
        log = self.query_one("#system-log", RichLog)
        log.write("[bold yellow][!] Channel filter dialog not yet implemented.[/bold yellow]")

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        bssid = event.row_key.value
        target_ap = self.ap_cache.get(bssid)
        if target_ap:
            # Stop the channel hopper before entering Focus Mode
            if self.app.active_interface:
                await self.app.active_interface.stop_hopping()
                
            self.app.target_ap = target_ap
            self.app.push_screen("focus")
