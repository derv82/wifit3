import time
from typing import Dict, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog
from rich.color import Color
from rich.markup import escape
from rich.style import Style
from rich.text import Span, Text

from wifit3.engine.models import AccessPoint

from ..capture_events import CaptureEvent, CaptureEventDetector
from ..encryption_format import format_encryption_markup
from .channel_filter import ChannelFilterDialog


# Rows fade their foreground toward the theme background over this duration,
# then get evicted on the next sort tick.
FADE_DURATION_S = 30.0

# Floor on row brightness — rows never fade past this fraction of original
# color before eviction. Keeps the final few seconds legible instead of
# punching black gaps in the table.
MIN_BRIGHTNESS = 0.2

# Sort the table on its own cadence instead of every value-update tick —
# stops rows from bouncing on every signal/beacon update. Same tick evicts
# fully-faded APs.
SORT_INTERVAL_S = 2.0


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _fade_text(text: Text, factor: float, bg: tuple[int, int, int]) -> Text:
    """Blend every span's foreground toward `bg` by `factor` (0..1)."""
    if factor <= 0:
        return text

    def _fade(style):
        if isinstance(style, str):
            style = Style.parse(style) if style else Style()
        if style.color is None:
            return style
        t = style.color.get_truecolor()
        s = 1.0 - factor
        return style + Style(color=Color.from_rgb(
            t.red * s + bg[0] * factor,
            t.green * s + bg[1] * factor,
            t.blue * s + bg[2] * factor,
        ))

    out = text.copy()
    out.style = _fade(out.style)
    out.spans = [Span(sp.start, sp.end, _fade(sp.style)) for sp in out.spans]
    return out


class ScannerView(Screen):
    """The main AP scanning list screen."""

    BINDINGS = [
        Binding("q", "app.quit", "Quit", show=True),
        Binding("c", "change_channel", "Channel Filter", show=True),
        Binding("s", "cycle_sort", "Sort Col", show=True),
        Binding("o", "toggle_sort_dir", "Sort Asc/Desc", show=True),
        Binding("l", "toggle_log", "Toggle Log", show=True),
        Binding("home", "scroll_home", "Top", show=False, priority=True),
        Binding("end", "scroll_end", "Bottom", show=False, priority=True),
    ]

    # (column_key, display_label). Order here = on-screen order.
    _COLUMNS = [
        ("bssid", "BSSID"),
        ("channel", "CH"),
        ("signal", "POWER"),
        ("beacons", "BEACONS"),
        ("clients", "#CLI"),
        ("encryption", "ENCRYPT"),
        ("ssid", "SSID"),
    ]

    # Columns whose values are right-aligned numerics.
    _RIGHT_ALIGNED = {"channel", "signal", "beacons", "clients"}

    def __init__(self):
        super().__init__()
        self.ap_cache: Dict[str, AccessPoint] = {}
        self._refresh_timer = None
        self._sort_timer = None
        # Default to POWER, descending (most signal at top).
        self._sort_idx = 2
        self._sort_reverse = True
        # None = hop on every channel the driver supports.
        self._channel_filter: Optional[List[int]] = None
        # Capture-event detector — coarse (no per-EAPOL spam in the scanner).
        self._events = CaptureEventDetector(granular_eapol=False)

    # ----- Compose / mount ---------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            table = DataTable(cursor_type="row", id="ap-table")
            for key, label in self._COLUMNS:
                table.add_column(label, key=key)
            yield table
            yield RichLog(id="system-log", markup=True, highlight=True)
        yield Footer()

    async def on_mount(self) -> None:
        log = self.query_one("#system-log", RichLog)
        log.write("[bold green]Scanner Initialized.[/bold green]")
        log.write(
            f"[dim]Rows fade out over {int(FADE_DURATION_S)}s of silence "
            f"(out of range / off-channel), then disappear.[/dim]"
        )
        self._update_column_headers()

        if self.app.active_interface:
            log.write(
                f"[cyan]Starting channel hopper on "
                f"{self.app.active_interface.name}...[/cyan]"
            )
            await self.app.active_interface.start_hopping(interval=0.25)
            # 15 FPS in-place value updates — no resort. Beacons arrive ~10 Hz
            # per AP at best, so 15 Hz is plenty and 4x cheaper than 60.
            self._refresh_timer = self.set_interval(1 / 15, self.refresh_table)
            # Lazy resort + evict expired APs.
            self._sort_timer = self.set_interval(
                SORT_INTERVAL_S, self._apply_sort_and_evict
            )

    # ----- Column header / sort indicator ------------------------------------

    def _update_column_headers(self) -> None:
        table = self.query_one("#ap-table", DataTable)
        sort_key, _ = self._COLUMNS[self._sort_idx]
        indicator = " ▼" if self._sort_reverse else " ▲"

        for key, base_label in self._COLUMNS:
            label = base_label + indicator if key == sort_key else base_label
            if key in table.columns:
                table.columns[key].label = label
        table.refresh()

    # ----- Per-tick refresh --------------------------------------------------

    def refresh_table(self) -> None:
        if not self.app.active_interface:
            return
        iface = self.app.active_interface
        table = self.query_one("#ap-table", DataTable)

        # Pre-compute per-AP client counts in a single pass over iface.clients
        # (avoids O(N×M) inside the AP loop below).
        client_counts: Dict[str, int] = {}
        for c in iface.clients.values():
            if c.bssid and c.mac not in iface.forged_macs:
                client_counts[c.bssid] = client_counts.get(c.bssid, 0) + 1

        now = time.time()
        tv = self.app.theme_variables
        bg = _hex_rgb(tv.get("background", "#000000"))
        # Cache resolved theme fg on self so _build_cells / _ssid_markup can
        # pick it up without threading params (refresh tick is the only caller).
        self._theme_fg = tv.get("foreground", "#ffffff")

        for ap in iface.get_access_points():
            age = now - ap.last_seen
            if age >= FADE_DURATION_S:
                # Eviction runs on the 2 s sort tick — don't drop mid-frame.
                continue

            n_cli = client_counts.get(ap.bssid, 0)
            # Quadratic ease-in: cells stay bright through the first half,
            # then fade fast. Capped at 1-MIN_BRIGHTNESS to stay readable.
            factor = min(1.0 - MIN_BRIGHTNESS, (age / FADE_DURATION_S) ** 2)
            cells = [
                _fade_text(c, factor, bg)
                for c in self._build_cells(ap, n_cli)
            ]

            if ap.bssid not in self.ap_cache:
                self.ap_cache[ap.bssid] = ap
                table.add_row(*cells, key=ap.bssid)
            else:
                # Decloak event — already logged here.
                old_ssid = self.ap_cache[ap.bssid].ssid
                if not old_ssid and ap.ssid:
                    self._write_log(
                        Text.from_markup(
                            f"[bold yellow][*] Decloaked Hidden Network: "
                            f"{escape(ap.bssid)} -> {escape(ap.ssid)}[/bold yellow]",
                            emoji=False,
                        )
                    )

                self.ap_cache[ap.bssid] = ap
                for (key, _), cell in zip(self._COLUMNS, cells):
                    table.update_cell(ap.bssid, key, cell)

            # Drain new capture events for this AP into the log.
            self._drain_capture_events(ap, iface.forged_macs)

    def _apply_sort_and_evict(self) -> None:
        """Re-sort the table and drop fully-faded APs. Runs every 2 s."""
        self._evict_expired_aps()
        self._apply_sort()

    def _evict_expired_aps(self) -> None:
        if not self.app.active_interface:
            return
        iface = self.app.active_interface
        table = self.query_one("#ap-table", DataTable)
        now = time.time()

        to_drop = [
            bssid for bssid, ap in self.ap_cache.items()
            if (now - ap.last_seen) >= FADE_DURATION_S
        ]
        for bssid in to_drop:
            iface.access_points.pop(bssid, None)
            self.ap_cache.pop(bssid, None)
            try:
                table.remove_row(bssid)
            except Exception:
                pass

    # ----- Cell construction -------------------------------------------------

    def _build_cells(self, ap: AccessPoint, n_clients: int) -> List[Text]:
        """Build the per-column full-color Text cells for one AP row.
        Aging is applied by the caller via `_fade_text`.

        Detail parens like `(PSK)` get theme-fg so they fade with the row
        rather than competing with row-age as a separate signal — the row
        fade is the AP's health indicator.
        """
        fg = self._theme_fg
        return [
            Text(ap.bssid, style=fg),
            Text(str(ap.channel), justify="right", style=fg),
            Text(f"{ap.signal} dBm", justify="right", style=fg),
            Text(str(ap.beacons), justify="right", style=fg),
            Text(str(n_clients) if n_clients else "", justify="right", style=fg),
            # style=fg gives the bare '→' between WPA3/WPA2 a fadeable base color.
            Text.from_markup(format_encryption_markup(ap, muted=fg), emoji=False, style=fg),
            self._ssid_markup(ap),
        ]

    def _ssid_markup(self, ap: AccessPoint) -> Text:
        """Bold for real SSIDs; italic '<Hidden>' otherwise. Same fg either
        way — italic alone signals the placeholder."""
        if ap.ssid:
            text = Text(ap.ssid, style=f"{self._theme_fg} bold")
        else:
            text = Text("<Hidden>", style=f"{self._theme_fg} italic")
        markers_markup = self._capture_marker_markup(ap)
        if markers_markup:
            text.append(" ")
            text.append_text(Text.from_markup(markers_markup, emoji=False))
        return text

    @staticmethod
    def _capture_marker_markup(ap: AccessPoint) -> str:
        """Rich-markup '[green]✓HS[/green] [green]✓PMK[/green]' string."""
        parts: List[str] = []
        has_hs = any(hs.is_complete for hs in ap.handshakes.values())
        has_pmk = any(hs.pmkid for hs in ap.handshakes.values())
        if has_hs:
            parts.append("[green]✓HS[/green]")
        if has_pmk:
            parts.append("[green]✓PMK[/green]")
        return " ".join(parts)

    # ----- Capture-event logging ---------------------------------------------

    def _drain_capture_events(self, ap: AccessPoint, forged_macs) -> None:
        for ev in self._events.poll(ap, forged_macs=forged_macs):
            self._log_capture_event(ev)

    def _log_capture_event(self, ev: CaptureEvent) -> None:
        ap_label = escape(ev.ssid or ev.bssid)
        client = escape(ev.client_mac)
        if ev.kind == "handshake_complete":
            pair = ev.pair_label or "?"
            msg = (
                f"[bold green]✓ HANDSHAKE[/bold green] ({pair}) on "
                f"[bold]{ap_label}[/bold] from [bold]{client}[/bold]"
            )
        elif ev.kind == "pmkid":
            msg = (
                f"[bold green]✓ PMKID[/bold green] on "
                f"[bold]{ap_label}[/bold] from [bold]{client}[/bold]"
            )
        else:
            return  # eapol events suppressed in scanner
        self._write_log(Text.from_markup(msg, emoji=False))

    def _write_log(self, text) -> None:
        try:
            log = self.query_one("#system-log", RichLog)
        except Exception:
            return
        log.write(text)

    # ----- Sort --------------------------------------------------------------

    def _apply_sort(self) -> None:
        table = self.query_one("#ap-table", DataTable)
        if table.row_count == 0:
            return

        try:
            current_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key
        except Exception:
            current_key = None

        sort_key, _ = self._COLUMNS[self._sort_idx]

        def _key(val):
            if isinstance(val, Text):
                val = val.plain
            s = str(val).strip()
            if not s:
                # Empty client-count cells sort to the bottom regardless of
                # direction by mapping to -inf in descending mode, but we
                # can't change direction here. Sentinel 0 is fine for now.
                return 0
            # Strip non-numeric suffix (e.g. " dBm")
            head = s.split()[0]
            try:
                return int(head)
            except ValueError:
                pass
            try:
                return float(head)
            except ValueError:
                pass
            return s.lower()

        table.sort(sort_key, key=_key, reverse=self._sort_reverse)

        if current_key:
            try:
                new_idx = table.get_row_index(current_key)
                table.move_cursor(row=new_idx, animate=False)
            except Exception:
                pass

    # ----- Actions -----------------------------------------------------------

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
        iface = self.app.active_interface
        if not iface:
            log.write("[bold red][!] No active interface.[/bold red]")
            return

        supported = getattr(iface.driver, "SUPPORTED_CHANNELS", None)
        if not supported:
            log.write(
                "[bold red][!] Driver did not declare SUPPORTED_CHANNELS.[/bold red]"
            )
            return

        dialog = ChannelFilterDialog(
            supported_channels=list(supported),
            current_filter=self._channel_filter,
        )
        self.app.push_screen(dialog, self._on_channel_filter_result)

    async def _on_channel_filter_result(
        self, result: Optional[List[int]]
    ) -> None:
        log = self.query_one("#system-log", RichLog)
        if result is None:
            log.write("[dim]Channel filter unchanged.[/dim]")
            return

        iface = self.app.active_interface
        if not iface:
            return

        self._channel_filter = result
        await iface.stop_hopping()
        dropped = self._prune_aps_outside(result)
        await iface.start_hopping(channels=result, interval=0.25)

        ch_24 = [c for c in result if c <= 14]
        ch_5 = [c for c in result if c > 14]
        parts = []
        if ch_24:
            parts.append(f"{len(ch_24)} on 2.4 GHz")
        if ch_5:
            parts.append(f"{len(ch_5)} on 5 GHz")
        summary = " + ".join(parts) if parts else f"{len(result)} channels"
        log.write(
            f"[bold green][+] Hopping {summary}:[/bold green] {result}"
        )
        if dropped:
            log.write(
                f"[dim]  Cleared {dropped} AP(s) outside the filter.[/dim]"
            )

    def _prune_aps_outside(self, channels: List[int]) -> int:
        iface = self.app.active_interface
        if not iface:
            return 0
        keep = set(channels)
        table = self.query_one("#ap-table", DataTable)

        stale = [
            bssid
            for bssid, ap in iface.access_points.items()
            if ap.channel not in keep
        ]
        for bssid in stale:
            iface.access_points.pop(bssid, None)
            self.ap_cache.pop(bssid, None)
            try:
                table.remove_row(bssid)
            except Exception:
                # Row may already be gone if a race occurred with refresh_table.
                pass
        return len(stale)

    async def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        bssid = event.row_key.value
        target_ap = self.ap_cache.get(bssid)
        if target_ap:
            if self.app.active_interface:
                await self.app.active_interface.stop_hopping()
            self.app.target_ap = target_ap
            self.app.push_screen("focus")
