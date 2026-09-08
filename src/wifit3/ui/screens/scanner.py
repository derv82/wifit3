import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from textual._two_way_dict import TwoWayDict
from textual.app import ComposeResult, RenderResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import Reactive
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog
from textual.widgets._header import HeaderClock, HeaderIcon, HeaderTitle
from rich.markup import escape
from rich.text import Text

from wifit3.campaigns import treelog
from wifit3.campaigns.pbc import PbcWatcher, WpsPbcCapture
from wifit3.campaigns.probe import probe_ap
from wifit3.campaigns.wps.registrar import PinResult
from wifit3.dot11.wsc.identity import WpsM1Identity
from wifit3.persist.capture_history import load_capture_index, summarize
from wifit3.persist.config import Config
from wifit3.models import AccessPoint, PersistedCapture
from wifit3.persist.save import save_handshake, save_pmkid, save_wps_pbc
from wifit3.crack.handshake import pmkid_crackable

from ..capture_events import (
    CAPTURE_TOAST_TITLES, DECLOAK_METHOD_LABELS, CaptureEvent, CaptureEventDetector, CaptureKind,
)
from ..encryption_format import format_encryption_markup, wep_key_ascii
from wifit3.wlan.channels import band_ranges

from .channel_filter import ChannelFilterDialog
from .filter import FilterBar, ScanFilter

if TYPE_CHECKING:
    from wifit3.ui.app import WifiteApp


STALE_DURATION_S = 10.0  # Seconds without a beacon before an AP row is dimmed.
EVICT_DURATION_S = 30.0  # Seconds without a beacon before an AP is dropped from the table.
FADE_DURATION_S = EVICT_DURATION_S

SORT_INTERVAL_S = 2.0  # Table sort delay
BEACON_DISPLAY_INTERVAL_S = 0.5


@dataclass(slots=True)
class _APRowState:
    signal: int
    beacons: int
    clients: int
    is_stale: bool
    flash: bool = False
    wps: bool = False
    wps_locked: bool = False
    ssid: Optional[str] = None
    chips_markup: str = ""
    identity: str = ""
    channel: int = 0
    encryption: str = ""


def device_scan_summary(members) -> Optional[str]:
    """The scanning pool as a log line: 'N devices: CHIP (2+5G)', 2.4 GHz cyan, 5 GHz green."""
    if not members:
        return None
    tags = []
    for m in members:
        lo = any(c <= 14 for c in m.supported_channels)
        hi = any(c > 14 for c in m.supported_channels)
        bands = []
        if lo:
            bands.append("[bold cyan]2[/]" if hi else "[bold cyan]2G[/]")
        if hi:
            bands.append("[bold green]5G[/]")
        tags.append(f"[bold]{m.chipset}[/] ({'+'.join(bands)})")
    noun = "device" if len(members) == 1 else "devices"
    return f"Scanning with [bold cyan]{len(members)}[/] {noun}: {', '.join(tags)}"


class _ChannelReadout(HeaderClock):
    """Header right slot: the live hopped channel(s), polled from the pool, not a clock."""
    DEFAULT_CSS = "_ChannelReadout { width: auto; }"
    # layout=True so a change re-sizes this auto-width slot; a plain repaint
    # leaves it 0-wide until the next resize.
    channels: Reactive[str] = Reactive("", layout=True)

    def _on_mount(self, event) -> None:
        self._poll()                          # populate before the first layout
        self.set_interval(0.25, self._poll)   # hop cadence

    def _poll(self) -> None:
        array = getattr(self.app, "array", None)
        members = array.members if array else []
        self.channels = " | ".join(f"CH:{m.current_channel:>3}" for m in members)

    def render(self) -> RenderResult:
        return Text(self.channels)


class _ScannerHeader(Header):
    """Header whose right slot shows the hopped channel(s) in place of the clock."""
    def compose(self) -> ComposeResult:
        yield HeaderIcon().data_bind(Header.icon)
        yield HeaderTitle()
        yield _ChannelReadout()


class _APScanTable(DataTable):
    """AP list table that can re-pin its row cursor without moving the viewport."""

    _suppress_scroll: bool = False

    def _scroll_cursor_into_view(self, animate: bool = False) -> None:
        if self._suppress_scroll:
            return
        super()._scroll_cursor_into_view(animate=animate)

    def pin_cursor_row(self, row: int) -> None:
        """Move the row cursor to ``row`` without scrolling the viewport."""
        self._suppress_scroll = True
        self.move_cursor(row=row, animate=False)
        self.call_after_refresh(self._release_scroll)

    def _release_scroll(self) -> None:
        self._suppress_scroll = False

    def sort_aps(self, sort_key: str, key_func: Callable[[str, Any], Any], reverse: bool) -> None:
        """Sort rows by key_func(row_key, cell_value)."""
        ordered_rows = sorted(
            self._data.items(),
            key=lambda r: key_func(r[0].value, r[1].get(sort_key)),
            reverse=reverse,
        )
        self._row_locations = TwoWayDict(
            {row_key: idx for idx, (row_key, _) in enumerate(ordered_rows)}
        )
        self._update_count += 1
        self.refresh()


class ScannerView(Screen):
    """The main AP scanning list screen."""

    app: "WifiteApp"

    BINDINGS = [
        Binding("q", "app.quit", "Quit", show=True),
        Binding("c", "change_channel", "Channel Filter", show=True),
        Binding("e", "focus_encryption", "Encryption", show=True),
        Binding("s", "cycle_sort", "Sort Col", show=True),
        Binding("o", "toggle_sort_dir", "Sort Asc/Desc", show=True),
        Binding("f", "focus_filter", "Filter", show=True),
        Binding("l", "toggle_log", "Toggle Log", show=True),
        Binding("w", "wps_pbc_mode", "WPS PBC", show=True),
        Binding("p", "probe_router_info", "Probe Info", show=True),
        Binding("home", "scroll_home", "Top", show=False, priority=True),
        Binding("end", "scroll_end", "Bottom", show=False, priority=True),
    ]

    # (column_key, display_label). Order here = on-screen order.
    _COLUMNS = [
        ("ssid", "SSID"),
        ("channel", "CH"),
        ("signal", "POWER"),
        ("beacons", "🥓"),
        ("clients", "💻"),
        ("encryption", "ENCRYPT"),
        ("wps", "WPS"),
        ("identity", "IDENTITY"),
    ]

    # Columns whose values are right-aligned in display.
    _RIGHT_ALIGNED = {"ssid", "channel", "signal", "beacons", "clients"}

    # Columns whose values are numeric for sorting.
    _NUMERIC_COLS = {"channel", "signal", "beacons", "clients"}

    # How long to flash the 🥓 cell when a beacon arrives.
    BEACON_FLASH_S = 0.2

    # Power deadband threshold (dBm) to prevent row flapping on RSSI noise.
    SORT_POWER_DEADBAND_DB = 3

    def __init__(self):
        super().__init__()
        self.ap_cache: Dict[str, AccessPoint] = {}
        self._refresh_timer = None
        self._sort_timer = None
        self._sort_idx = 2         # Default to POWER
        self._sort_reverse = True  # Descending
        self._channel_filter: Optional[List[int]] = None
        self._scan_filter: ScanFilter = ScanFilter()
        self._events = CaptureEventDetector(granular_eapol=False)
        # Per-BSSID prev-beacon-count + flash-deadline for "beacon arrived"
        # cell highlight.
        self._prev_beacons: Dict[str, int] = {}
        self._beacon_flash_until: Dict[str, float] = {}
        # Throttled 🥓 count actually shown, per BSSID: (value, last-stepped-at).
        self._beacon_shown: Dict[str, tuple[int, float]] = {}
        # Per-BSSID dynamic row state and sort power.
        self._row_states: Dict[str, _APRowState] = {}
        self._sort_power: Dict[str, int] = {}
        self._last_user_nav: float = 0.0
        # captures/ history, loaded once at mount and hydrated onto APs by
        # BSSID so previously-saved handshakes/PMKIDs/WEP keys re-badge.
        self._capture_index: Dict[str, List[PersistedCapture]] = {}
        # WPS PBC auto-invade. ON by default. The enabled flag lives on the app
        # (app.pbc_enabled). Watcher + capturing serialization stay Scanner-local.
        self._pbc_watcher = PbcWatcher()
        self._pbc_capturing = False          # serialize: one invade at a time
        self._router_info_probing = False
        self._router_info_probe_started_at: Optional[float] = None
        self._router_info_probe_bssid: Optional[str] = None

    # ----- Compose / mount ---------------------------------------------------

    def compose(self) -> ComposeResult:
        yield _ScannerHeader()
        array = self.app.array
        supported = list(array.supported_channels) if array else []
        with Vertical():
            yield FilterBar(supported)
            table = _APScanTable(cursor_type="row", id="ap-table")
            for key, label in self._COLUMNS:
                # Reserve 2 chars in every header to account for sort indicator
                table.add_column(label + "  ", key=key)
            yield table
            yield RichLog(id="system-log", markup=True, highlight=True)
        yield Footer()

    async def on_mount(self) -> None:
        log = self.query_one("#system-log", RichLog)
        scanner_sort = "identity" if Config.scanner_sort in ("vendor", "brand") else Config.scanner_sort
        self._sort_idx = next(
            (i for i, (key, _label) in enumerate(self._COLUMNS) if key == scanner_sort), 2)
        self._sort_reverse = Config.scanner_sort_reverse
        self._update_column_headers()
        self.query_one("#ap-table", DataTable).focus()
        array = self.app.array

        log.write(treelog.header("Scanner initialized"))
        rows: List[str] = []
        summary = self._load_capture_history()
        if summary:
            rows.append(summary)
        if array:
            device_line = device_scan_summary(array.members)
            if device_line:
                rows.append(device_line)
        else:
            rows.append("[yellow]No active interface[/yellow]")
        for i, row in enumerate(rows):
            log.write(treelog.leaf(row) if i == len(rows) - 1 else treelog.branch(row))

        if array:
            # 15 FPS in-place value updates. Beacons arrive ~10 Hz per AP at best.
            self._refresh_timer = self.set_interval(1 / 15, self.refresh_table)
            # Lazy re-sort + evict expired APs.
            self._sort_timer = self.set_interval(
                SORT_INTERVAL_S, self._apply_sort_and_evict
            )
            self._pbc_timer = self.set_interval(1.0, self._poll_pbc)
            self._log_pbc_status()  # Auto-invade is ON by default

    def _load_capture_history(self) -> Optional[str]:
        """Load captures/ once; return the one-line summary (None if empty)."""
        self._capture_index = load_capture_index()
        return self._format_history_summary(*summarize(self._capture_index))

    @staticmethod
    def _format_history_summary(hs: int, pmkid: int, wep: int, wps: int) -> Optional[str]:
        """`Existing captures/: N handshakes, N PMKIDs, N WEP keys, N WPS PSKs`."""
        parts = []
        if hs:
            parts.append(f"{hs} handshake{'s' * (hs != 1)}")
        if pmkid:
            parts.append(f"{pmkid} PMKID{'s' * (pmkid != 1)}")
        if wep:
            parts.append(f"{wep} WEP key{'s' * (wep != 1)}")
        if wps:
            parts.append(f"{wps} WPS PSK{'s' * (wps != 1)}")
        if not parts:
            return None
        return f"Existing [bold]{Config.captures_dir}/[/bold]: " + ", ".join(parts)

    async def on_screen_resume(self) -> None:
        # Restart channel hopper
        array = self.app.array
        if not array:
            return
        await array.start_hopping(
            channels=self._channel_filter, interval=0.25
        )

    # ----- Column header / sort indicator ------------------------------------

    def _update_column_headers(self) -> None:
        table = self.query_one("#ap-table", DataTable)
        sort_key, _ = self._COLUMNS[self._sort_idx]
        arrow = "▼" if self._sort_reverse else "▲"

        for key, base_label in self._COLUMNS:
            is_sorted = key == sort_key
            if key in self._RIGHT_ALIGNED:
                # Right-align column header to rows
                prefix = f"{arrow} " if is_sorted else "  "
                label = Text(prefix + base_label, justify="right")
            else:
                # Left-aligned text columns: arrow trails the label.
                suffix = f" {arrow}" if is_sorted else "  "
                label = Text(base_label + suffix, justify="left")
            if key in table.columns:
                table.columns[key].label = label
        table.refresh()

    # ----- Per-tick refresh --------------------------------------------------

    def refresh_table(self) -> None:
        if not self.app.array:
            return
        array = self.app.array
        table = self.query_one("#ap-table", DataTable)

        # Pre-compute per-AP client counts to avoid O(N×M) inside the AP loop below.
        client_counts: Dict[str, int] = {}
        for c in array.clients.values():
            if c.bssid and c.mac not in array.forged_macs:
                client_counts[c.bssid] = client_counts.get(c.bssid, 0) + 1

        now = time.time()
        self._theme_fg = self.app.theme_variables.get("foreground", "#ffffff")

        for ap in array.get_access_points(include_eviltwin=False):
            guessed_ssid = (
                self._best_named_sibling_ssid(ap)
                if self._scan_filter.text and ap.ssid is None
                else None
            )
            if not self._scan_filter.matches(ap, ssid=guessed_ssid):
                if ap.bssid in self.ap_cache:
                    self._forget_row(ap.bssid, drop_from_array=False)
                continue

            age = self._ap_row_age(ap, now)
            if age >= EVICT_DURATION_S:
                continue

            if not ap.persisted:
                hist = self._capture_index.get(ap.bssid)
                if hist:
                    ap.persisted = hist

            is_stale = age > STALE_DURATION_S
            n_cli = client_counts.get(ap.bssid, 0)

            # Update sort deadband
            sp = self._sort_power.get(ap.bssid)
            if sp is None or abs(ap.signal - sp) >= self.SORT_POWER_DEADBAND_DB:
                self._sort_power[ap.bssid] = ap.signal

            # Beacon-arrival flash: bump the deadline when beacon count changes.
            prev = self._prev_beacons.get(ap.bssid)
            if prev is not None and ap.beacons > prev:
                self._beacon_flash_until[ap.bssid] = now + self.BEACON_FLASH_S
            self._prev_beacons[ap.bssid] = ap.beacons
            flash_bacon = now < self._beacon_flash_until.get(ap.bssid, 0.0)

            shown_beacons = self._display_beacons(ap, now)
            chips_markup = self._ssid_chips_markup(ap)
            enc_markup = format_encryption_markup(ap, muted=self._theme_fg)
            ident_summary = ap.identity.summary

            prev_state = self._row_states.get(ap.bssid)
            if prev_state is None:
                self.ap_cache[ap.bssid] = ap
                self._row_states[ap.bssid] = _APRowState(
                    signal=ap.signal,
                    beacons=shown_beacons,
                    clients=n_cli,
                    is_stale=is_stale,
                    flash=flash_bacon,
                    wps=ap.wps,
                    wps_locked=ap.wps_locked,
                    ssid=ap.ssid,
                    chips_markup=chips_markup,
                    identity=ident_summary,
                    channel=ap.channel,
                    encryption=enc_markup,
                )
                row_cells = [
                    self._render_cell(
                        ap, col_k, is_stale, n_cli=n_cli,
                        flash_bacon=flash_bacon, shown_beacons=shown_beacons,
                    )
                    for col_k, _ in self._COLUMNS
                ]
                table.add_row(*row_cells, key=ap.bssid)
            else:
                self.ap_cache[ap.bssid] = ap

                # Decloak event: already logged here.
                if not prev_state.ssid and ap.ssid:
                    self._write_log(
                        Text.from_markup(
                            f"[bold yellow][*] Decloaked Hidden Network: "
                            f"{escape(ap.bssid)} -> {escape(ap.ssid)}[/bold yellow]",
                            emoji=False,
                        )
                    )

                if prev_state.is_stale != is_stale:
                    prev_state.is_stale = is_stale
                    prev_state.signal = ap.signal
                    prev_state.beacons = shown_beacons
                    prev_state.clients = n_cli
                    prev_state.flash = flash_bacon
                    prev_state.wps = ap.wps
                    prev_state.wps_locked = ap.wps_locked
                    prev_state.ssid = ap.ssid
                    prev_state.chips_markup = chips_markup
                    prev_state.identity = ident_summary
                    prev_state.channel = ap.channel
                    prev_state.encryption = enc_markup
                    for col_k, _ in self._COLUMNS:
                        cell = self._render_cell(
                            ap, col_k, is_stale, n_cli=n_cli,
                            flash_bacon=flash_bacon, shown_beacons=shown_beacons,
                        )
                        table.update_cell(ap.bssid, col_k, cell)
                else:
                    if prev_state.ssid != ap.ssid or prev_state.chips_markup != chips_markup:
                        prev_state.ssid = ap.ssid
                        prev_state.chips_markup = chips_markup
                        table.update_cell(ap.bssid, "ssid", self._render_cell(ap, "ssid", is_stale))

                    if prev_state.channel != ap.channel:
                        prev_state.channel = ap.channel
                        table.update_cell(ap.bssid, "channel", self._render_cell(ap, "channel", is_stale))

                    if prev_state.signal != ap.signal:
                        prev_state.signal = ap.signal
                        table.update_cell(ap.bssid, "signal", self._render_cell(ap, "signal", is_stale))

                    if prev_state.beacons != shown_beacons or prev_state.flash != flash_bacon:
                        prev_state.beacons = shown_beacons
                        prev_state.flash = flash_bacon
                        table.update_cell(
                            ap.bssid, "beacons",
                            self._render_cell(ap, "beacons", is_stale, flash_bacon=flash_bacon, shown_beacons=shown_beacons),
                        )

                    if prev_state.clients != n_cli:
                        prev_state.clients = n_cli
                        table.update_cell(
                            ap.bssid, "clients",
                            self._render_cell(ap, "clients", is_stale, n_cli=n_cli),
                        )

                    if prev_state.encryption != enc_markup:
                        prev_state.encryption = enc_markup
                        table.update_cell(ap.bssid, "encryption", self._render_cell(ap, "encryption", is_stale))

                    if prev_state.wps != ap.wps or prev_state.wps_locked != ap.wps_locked:
                        prev_state.wps = ap.wps
                        prev_state.wps_locked = ap.wps_locked
                        table.update_cell(ap.bssid, "wps", self._render_cell(ap, "wps", is_stale))

                    if prev_state.identity != ident_summary:
                        prev_state.identity = ident_summary
                        table.update_cell(ap.bssid, "identity", self._render_cell(ap, "identity", is_stale))

            self._drain_capture_events(ap, array.forged_macs)

    def _apply_sort_and_evict(self) -> None:
        """Re-sort the table and drop fully-faded APs. Runs every 2 s."""
        self._evict_expired_aps()
        self._apply_sort(scroll_to_cursor=False, force=False)

    def _evict_expired_aps(self) -> None:
        if not self.app.array:
            return
        now = time.time()
        to_drop = [
            bssid for bssid, ap in self.ap_cache.items()
            if self._ap_row_age(ap, now) >= EVICT_DURATION_S
        ]
        for bssid in to_drop:
            self._forget_row(bssid, drop_from_array=True)

    def _ap_row_age(self, ap: AccessPoint, now: float) -> float:
        freeze_at = self._router_info_probe_started_at if self._router_info_probing else None
        age_at = freeze_at if freeze_at is not None else now
        return max(0.0, age_at - ap.last_seen)

    def _forget_row(self, bssid: str, *, drop_from_array: bool) -> None:
        """Drop the AP's row and caches; drop_from_array also evicts it from the registry."""
        if drop_from_array and self.app.array:
            self.app.array.access_points.pop(bssid, None)
        self.ap_cache.pop(bssid, None)
        self._prev_beacons.pop(bssid, None)
        self._beacon_flash_until.pop(bssid, None)
        self._beacon_shown.pop(bssid, None)
        self._row_states.pop(bssid, None)
        self._sort_power.pop(bssid, None)
        try:
            self.query_one("#ap-table", DataTable).remove_row(bssid)
        except Exception:
            pass

    # ----- Cell construction -------------------------------------------------

    def _display_beacons(self, ap: AccessPoint, now: float) -> int:
        """The 🥓 count to show: the real count, but stepped at most every
        BEACON_DISPLAY_INTERVAL_S so a steadily-beaconing row holds still between
        steps. Steps immediately if the count dropped (counter reset / new AP)."""
        shown = self._beacon_shown.get(ap.bssid)
        if (shown is None or (now - shown[1]) >= BEACON_DISPLAY_INTERVAL_S
                or ap.beacons < shown[0]):
            self._beacon_shown[ap.bssid] = (ap.beacons, now)
            return ap.beacons
        return shown[0]

    def _render_cell(
        self, ap: AccessPoint, col_key: str, is_stale: bool,
        n_cli: int = 0, flash_bacon: bool = False, shown_beacons: int = 0,
    ) -> Text:
        """Build the Text renderable for a single column cell."""
        fg = self._theme_fg
        dim = "dim " if is_stale else ""
        if col_key == "ssid":
            cell = self._ssid_cell(ap)
            if is_stale:
                cell.stylize("dim")
            return cell
        if col_key == "channel":
            return Text(str(ap.channel), justify="right", style=f"{dim}{fg}")
        if col_key == "signal":
            return Text(f"{ap.signal} dBm", justify="right", style=f"{dim}{fg}")
        if col_key == "beacons":
            style = f"{dim}{fg} bold" if flash_bacon else f"{dim}{fg}"
            return Text(str(shown_beacons), justify="right", style=style)
        if col_key == "clients":
            return Text(str(n_cli) if n_cli else "", justify="right", style=f"{dim}{fg}")
        if col_key == "encryption":
            cell = Text.from_markup(format_encryption_markup(ap, muted=fg), emoji=False, style=fg)
            if is_stale:
                cell.stylize("dim")
            return cell
        if col_key == "wps":
            if ap.wps:
                label = "WPS 🔒" if ap.wps_locked else "WPS"
                return Text(label, style=f"{dim}{fg}")
            return Text("", style=f"{dim}{fg}")
        if col_key == "identity":
            return self._identity_cell(ap, is_stale)
        return Text("")

    def _identity_cell(self, ap: AccessPoint, is_stale: bool = False) -> Text:
        fg = self._theme_fg
        dim = "dim " if is_stale else ""
        text = ap.identity.summary
        return Text(text, style=f"{dim}{fg}")

    def _build_cells(
        self, ap: AccessPoint, n_clients: int, flash_bacon: bool = False,
        beacons_display: Optional[int] = None, is_stale: bool = False,
    ) -> List[Text]:
        """Build all column cells for one AP row (order matches _COLUMNS)."""
        shown = ap.beacons if beacons_display is None else beacons_display
        return [
            self._render_cell(
                ap, col_k, is_stale, n_cli=n_clients,
                flash_bacon=flash_bacon, shown_beacons=shown,
            )
            for col_k, _ in self._COLUMNS
        ]

    # Cap the SSID+badges cell so the capture badges never overflow.
    _SSID_CELL_MAX = 32

    def _ssid_cell(self, ap: AccessPoint) -> Text:
        """badges (left) + name (bold=named, italic=hidden, +'?'=sibling guess), right-aligned."""
        if ap.ssid:
            name = Text(ap.ssid, style=f"{self._theme_fg} bold")
        else:
            sib = self._best_named_sibling_ssid(ap)
            name = Text(f"{sib}?" if sib else "<Hidden>", style=f"{self._theme_fg} italic")

        chips_markup = self._ssid_chips_markup(ap)  # ✗S, ✓HS, ✓PMK, ✓WEP, ✓WPS
        chips_text = Text.from_markup(chips_markup, emoji=False) if chips_markup else None
        reserved = 1 + chips_text.cell_len if chips_text else 0   # 1 = separator space
        name.truncate(max(1, self._SSID_CELL_MAX - reserved), overflow="ellipsis")
        if chips_text:
            out = Text(justify="right")
            out.append_text(chips_text)
            out.append(" ")
            out.append_text(name)
        else:
            out = name
            out.justify = "right"
        return out

    def _best_named_sibling_ssid(self, ap: AccessPoint) -> Optional[str]:
        """Guess the sibling SSID to display for a hidden AP."""
        array = self.app.array
        if not array or not ap.siblings:
            return None
        best_ssid: Optional[str] = None
        best_beacons = -1
        for sib_bssid in ap.siblings:
            sib_ap = array.access_points.get(sib_bssid)
            if sib_ap and sib_ap.ssid and sib_ap.beacons > best_beacons:
                best_ssid = sib_ap.ssid
                best_beacons = sib_ap.beacons
        return best_ssid

    @staticmethod
    def _ssid_chips_markup(ap: AccessPoint) -> str:
        """Badges to the left of SSID for HS, PMK, WEP, WPS, silenced."""
        types = {p.type for p in ap.persisted}
        has_hs  = "HS"    in types or any(hs.is_complete for hs in ap.handshakes.values())
        has_pmk = "PMKID" in types or any(hs.pmkid and pmkid_crackable(hs) for hs in ap.handshakes.values())
        has_wep = "WEP"   in types or ap.wep_key is not None
        has_wps = "WPS"   in types or ap.wps_pbc_psk is not None
        silent = Config.is_silenced(ap.bssid)
        badges = [
            (silent, "[red]✗S[/red]"),
            (has_hs, "[green]✓HS[/green]"),
            (has_pmk, "[green]✓PMK[/green]"),
            (has_wep, "[green]✓WEP[/green]"),
            (has_wps, "[green]✓WPS[/green]"),
        ]
        return " ".join(text for cond, text in badges if cond)

    # ----- Capture-event logging ---------------------------------------------

    def _drain_capture_events(self, ap: AccessPoint, forged_macs) -> None:
        if Config.is_silenced(ap.bssid):
            return
        for ev in self._events.poll(ap, forged_macs=forged_macs):
            self._log_capture_event(ev, ap)

    def _log_capture_event(self, ev: CaptureEvent, ap: AccessPoint) -> None:
        ap_label = escape(ev.ssid or ev.bssid)
        client = escape(ev.client_mac)
        save_result = None
        if ev.kind == CaptureKind.HANDSHAKE:
            pair = ev.pair_label or "?"
            msg = (
                f"[bold green]✓ HANDSHAKE[/bold green] ({pair}) on "
                f"[bold cyan]{ap_label}[/bold cyan] from [bold]{client}[/bold]"
            )
            save_result = save_handshake(ap, ev.client_mac)
        elif ev.kind == CaptureKind.UNCRACKABLE_HANDSHAKE:
            msg = (
                f"[bold yellow]● {escape(ev.value or '?')} 4-way[/bold yellow] on "
                f"[bold cyan]{ap_label}[/bold cyan] [dim](not crackable, -m 22000)[/dim]"
            )
        elif ev.kind == CaptureKind.PMKID:
            msg = (
                f"[bold green]✓ PMKID[/bold green] on "
                f"[bold cyan]{ap_label}[/bold cyan] from [bold]{client}[/bold]"
            )
            save_result = save_pmkid(ap, ev.client_mac)
        elif ev.kind == CaptureKind.DECLOAK:
            # A ● header (not a ✓ win): a hidden SSID became visible, not a credential.
            method_label = DECLOAK_METHOD_LABELS.get(ev.method or "", ev.method or "?")
            self._write_log(Text.from_markup(treelog.header(
                f"[bold]Decloaked[/bold] [cyan]{escape(ev.bssid)}[/cyan] → "
                f"[green]{escape(ev.ssid or '')}[/green] "
                f"[dim]via {method_label}[/dim]"), emoji=False))
            return
        elif ev.kind == CaptureKind.WEP_KEY:
            msg = (f"[bold green]✓ WEP KEY[/bold green] on "
                   f"[bold cyan]{ap_label}[/bold cyan] = {escape(wep_key_ascii(ev.value or ''))}")
        elif ev.kind == CaptureKind.WPS_PIN:
            msg = (f"[bold green]✓ WPS PIN[/bold green] on "
                   f"[bold cyan]{ap_label}[/bold cyan] = {escape(ev.value or '')}")
        elif ev.kind == CaptureKind.WPS_PSK:
            msg = (f'[bold green]✓ WPS PSK[/bold green] on '
                   f'[bold cyan]{ap_label}[/bold cyan] = "{escape(ev.value or "")}"')
        elif ev.kind == CaptureKind.WPS_PBC:
            msg = (f'[bold green]✓ WPS PSK[/bold green] [dim](via PushButton)[/dim] on '
                   f'[bold cyan]{ap_label}[/bold cyan] = "{escape(ev.value or "")}"')
        else:
            return  # eapol events suppressed in scanner
        # Leading space aligns the ✓ win with the ● / ├─► / └─► tree log above it.
        self._write_log(Text.from_markup(f" {msg}", emoji=False))
        if save_result is not None:
            verb = "saved" if save_result.was_new else "already saved as"
            self._write_log(Text.from_markup(treelog.leaf(
                f"[dim]({verb} {escape(save_result.path.name)})[/dim]"), emoji=False))
        title = CAPTURE_TOAST_TITLES.get(ev.kind)
        if title:
            name = ev.ssid or ev.bssid
            if ev.kind == CaptureKind.WEP_KEY:
                self.notify(f"{name}: {wep_key_ascii(ev.value or '')}", title=title, timeout=6)
            else:
                pair = ev.pair_label or ("M1" if ev.kind == CaptureKind.PMKID else None)
                full_title = f"{title} ({pair})" if pair else title
                body = (f"[bold]{escape(name)}[/bold] on channel [bold]{ap.channel}[/bold] "
                        f"[dim bold](BSSID: {escape(ap.bssid)})[/dim bold]")
                self.notify(body, title=full_title, timeout=6)

    def _write_log(self, text) -> None:
        try:
            log = self.query_one("#system-log", RichLog)
        except Exception:
            return
        # Bypass RichLog's emojis (would turn :ab: / :cd: inside a BSSID into 🆎 / 💿).
        if isinstance(text, str):
            text = Text.from_markup(text, emoji=False)
        log.write(text)

    # ----- Sort --------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table = self.query_one("#ap-table", _APScanTable)
        if not table._suppress_scroll:
            self._last_user_nav = time.time()

    def _apply_sort(self, *, scroll_to_cursor: bool = True, force: bool = False) -> None:
        """Re-sort the table, maintains selected item.
        ``scroll_to_cursor`` controls whether the viewport follows the cursor.
        ``force`` bypasses the user navigation pause."""
        now = time.time()
        if not force and (now - self._last_user_nav) < 3.0:
            return

        table = self.query_one("#ap-table", _APScanTable)
        if table.row_count == 0:
            return

        try:
            current_key = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key
        except Exception:
            current_key = None

        sort_key, _ = self._COLUMNS[self._sort_idx]
        reverse = self._sort_reverse
        is_numeric_col = sort_key in self._NUMERIC_COLS

        def _key(bssid: str, val: Any) -> tuple:
            if sort_key == "signal":
                primary = self._sort_power.get(bssid, -100)
                return (0, primary)
            if sort_key == "ssid":
                ap = self.ap_cache.get(bssid)
                name = (ap.ssid or "") if ap else ""
                sentinel = int(not name != reverse)
                return (sentinel, name.lower())

            if isinstance(val, Text):
                val = val.plain
            s = str(val).strip() if val is not None else ""
            is_empty = not s

            if is_empty:
                primary: object = 0 if is_numeric_col else ""
            elif is_numeric_col:
                head = s.split()[0]
                try:
                    primary = int(head)
                except ValueError:
                    try:
                        primary = float(head)
                    except ValueError:
                        primary = float("inf") if not reverse else float("-inf")
            else:
                primary = s.lower()

            sentinel = int(is_empty != reverse)
            return (sentinel, primary)

        table.sort_aps(sort_key, key_func=_key, reverse=reverse)

        if current_key:
            try:
                new_idx = table.get_row_index(current_key)
                if scroll_to_cursor:
                    table.move_cursor(row=new_idx, animate=False)
                else:
                    table.pin_cursor_row(new_idx)
            except Exception:
                pass

    # ----- Actions -----------------------------------------------------------

    def action_toggle_log(self) -> None:
        log_widget = self.query_one("#system-log")
        log_widget.display = not log_widget.display

    def _selected_ap(self) -> Optional[AccessPoint]:
        table = self.query_one("#ap-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None
        return self.ap_cache.get(row_key)

    def action_probe_router_info(self) -> None:
        ap = self._selected_ap()
        if ap is None:
            self._write_log(treelog.leaf_fail("select an AP before probing identity"))
            return
        if not self.app.array:
            self._write_log(treelog.leaf_fail("no active interface"))
            return
        if self._router_info_probing or self._pbc_capturing:
            self._write_log(treelog.leaf_fail("another probe is already running"))
            return
        asyncio.create_task(self._probe_router_info(ap))

    async def _probe_router_info(self, ap: AccessPoint) -> None:
        array = self.app.array
        if not array:
            return
        self._router_info_probing = True
        self._router_info_probe_started_at = time.time()
        self._router_info_probe_bssid = ap.bssid
        label = escape(ap.ssid or ap.bssid)
        self._write_log(treelog.header(
            f"[bold]Identity probe[/bold] on [cyan]{label}[/cyan] [dim](CH {ap.channel})[/dim]"))
        iface = array.select_iface(ap.channel)
        if iface is None:
            self._write_log(treelog.leaf_fail(f"no interface can probe CH {ap.channel}"))
            self._router_info_probing = False
            return
        try:
            async with array.claim(iface):
                result = await probe_ap(iface, ap)
                if result.ok:
                    fields = self._format_probe_result(result)
                    self._write_log(treelog.leaf_ok(fields or "identity probe matched"))
                    self.refresh_table()
                else:
                    self._write_log(treelog.leaf_fail(
                        f"identity probe failed [dim]({escape(result.detail or 'no detail')})[/dim]"))
        except Exception as exc:
            self._write_log(treelog.leaf_fail(f"identity probe error: {escape(str(exc))}"))
        finally:
            self._router_info_probing = False
            self._router_info_probe_started_at = None
            self._router_info_probe_bssid = None

    @staticmethod
    def _format_probe_result(result) -> str:
        if result.wps_identity is not None:
            fields = ScannerView._format_wps_m1_identity(result.wps_identity)
            return f"WSC M1: {fields}" if fields else "WSC M1 received"
        if result.vendor:
            return f"{result.source}: {result.vendor}"
        return result.source or "identity probe matched"

    @staticmethod
    def _format_wps_m1_identity(identity: WpsM1Identity) -> str:
        parts = [
            ("mfr", identity.manufacturer),
            ("model", identity.model_name),
            ("model_no", identity.model_number),
            ("name", identity.device_name),
        ]
        return ", ".join(f"{name}={escape(value)}" for name, value in parts if value)

    # ----- WPS PBC opportunistic capture -------------------------------------

    def action_wps_pbc_mode(self) -> None:
        """Toggle WPS PBC auto-invade on/off (ON by default)."""
        self.app.pbc_enabled = not self.app.pbc_enabled
        self._log_pbc_status()
        if self.app.pbc_enabled:
            self._arm_open_windows()

    def _arm_open_windows(self) -> None:
        """React to PBC windows that are *already* open at the instant we arm."""
        array = self.app.array
        if not array:
            return
        launched = self._pbc_capturing
        for ap in array.get_access_points():
            if not ap.wps_pbc_active:
                continue
            if ap.has_psk:
                ssid = escape(ap.ssid or ap.bssid)
                self._write_log(f"  [dim]({ssid} already captured, PSK: [bold]{escape(ap.known_psk or '?')}[/bold])[/dim]")
            elif not launched:
                launched = True
                self._on_pbc_window(ap)

    def _log_pbc_status(self) -> None:
        """WPS PBC auto-invade state as a ● header + detail leaf. Shared by
        startup + the 'w' toggle."""
        if self.app.pbc_enabled:
            self._write_log(treelog.header(
                "[bold]WPS PushButton Extraction[/bold] is "
                "[bold green]enabled[/bold green] [dim](press [bold]w[/bold] to toggle)[/dim]",
                color="green"))
            self._write_log(treelog.leaf(
                "[dim](automatically retrieves PSK when [bold italic]any[/bold italic] "
                "WPS button is pressed)[/dim]"))
        else:
            self._write_log(treelog.header(
                "[bold]WPS PushButton Extraction[/bold] is "
                "[orange1]disabled[/orange1] [dim](detect only, press [bold]w[/bold] to toggle)[/dim]",
                color="orange1"))

    def _poll_pbc(self) -> None:
        array = self.app.array
        if not array or self.app.screen is not self:
            return
        for ap in self._pbc_watcher.new_windows(array.get_access_points()):
            self._on_pbc_window(ap)

    def _on_pbc_window(self, ap: AccessPoint) -> None:
        if Config.is_silenced(ap.bssid):
            return
        label = escape(ap.ssid or ap.bssid)
        self._write_log(
            f"[bold cyan]WPS PushButton [italic]auto-invade:[/italic][/bold cyan] "
            f"[bold green]Open Window[/bold green] on [bold]{label}[/bold] "
            f"[dim](CH {ap.channel})[/dim]")
        if not self.app.pbc_enabled:
            self._write_log(treelog.leaf("[dim]auto-invade off: press [bold]w[/bold] to enable[/dim]"))
            return
        if ap.has_psk:
            wps = next((p for p in ap.persisted if p.type == "WPS" and p.value), None)
            where = f" [dim]({escape(Path(wps.path).name)})[/dim]" if wps else ""
            self._write_log(treelog.leaf(f"[italic]already captured[/italic]{where}"))
            return
        if self._pbc_capturing:
            return
        asyncio.create_task(self._invade_pbc(ap))

    async def _invade_pbc(self, ap: AccessPoint) -> None:
        """Pause hop → tune to the target → run the PBC enrollment → resume."""
        array = self.app.array
        if not array:
            return
        self._pbc_capturing = True
        label = escape(ap.ssid or ap.bssid)
        self._write_log(treelog.branch(
            f"[cyan]invading[/cyan] [bold]{label}[/bold]: pausing hop, "
            f"tuning [cyan]CH {ap.channel}[/cyan]…"))
        try:
            await array.stop_hopping()
            await array.set_channel(ap.channel)
            outcome = await WpsPbcCapture(
                array, ap, log=lambda m: self._write_log(treelog.branch(m))
            ).capture()
            if outcome.result is PinResult.SUCCESS:
                ap.wps_pbc_psk = outcome.psk
                name = escape(outcome.ssid or ap.ssid or ap.bssid)
                self._write_log(treelog.branch_ok(
                    f"[black bold on cyan] PSK for {name}: \"{escape(outcome.psk)}\" [/black bold on cyan]"))
                try:
                    result = save_wps_pbc(ap, outcome.psk)
                    if result is None:
                        self._write_log(treelog.leaf("[dim](PSK not saved to disk)[/dim]"))
                    else:
                        verb = "saved" if result.was_new else "already saved as"
                        self._write_log(treelog.leaf(
                            f"[cyan]{verb}[/cyan] [dim]{escape(result.path.name)}[/dim]"))
                except Exception:
                    self._write_log(treelog.leaf("[dim](PSK not saved to disk)[/dim]"))
            else:
                self._write_log(treelog.leaf_fail(
                    f"{outcome.result.value} [dim]({escape(outcome.detail)})[/dim]"))
        except Exception as exc:                       # never let an invade kill the scanner
            self._write_log(treelog.leaf_fail(f"capture error: {escape(str(exc))}"))
        finally:
            self._pbc_capturing = False
            if self.app.screen is self:
                # Resume hopping only if we're still the foreground screen (not Focus).
                await array.start_hopping(channels=self._channel_filter, interval=0.25)

    def action_focus_filter(self) -> None:
        self.query_one(FilterBar).focus_text()

    def action_focus_encryption(self) -> None:
        self.query_one(FilterBar).focus_encryption()

    def action_cycle_sort(self) -> None:
        self._sort_idx = (self._sort_idx + 1) % len(self._COLUMNS)
        Config.scanner_sort = self._COLUMNS[self._sort_idx][0]
        self.app.persist_config()
        self._update_column_headers()
        self._apply_sort(force=True)

    def action_toggle_sort_dir(self) -> None:
        self._sort_reverse = not self._sort_reverse
        Config.scanner_sort_reverse = self._sort_reverse
        self.app.persist_config()
        self._update_column_headers()
        self._apply_sort(force=True)

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
        array = self.app.array
        if not array:
            log.write("[bold red][!] No active interface.[/bold red]")
            return

        supported = array.supported_channels
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
        if result is None:
            self.query_one("#system-log", RichLog).write("[dim]Channel filter unchanged.[/dim]")
        else:
            await self._apply_channel_filter(result)
        self.query_one(FilterBar).set_channels(self._channel_filter)
        self.query_one("#ap-table", DataTable).focus()

    async def _apply_channel_filter(self, channels: List[int]) -> None:
        """Re-point the hopper; a full-band pick becomes None so hotplug keeps re-spreading it."""
        array = self.app.array
        if not array:
            return
        full_band = set(channels) == set(array.supported_channels)
        self._channel_filter = None if full_band else channels
        await array.stop_hopping()
        dropped = self._prune_aps_outside(channels)
        await array.start_hopping(channels=self._channel_filter, interval=0.25)

        log = self.query_one("#system-log", RichLog)
        pieces = [
            f"[bold cyan]{name}[/bold cyan] [dim]({rngs})[/dim]"
            for name, rngs in band_ranges(channels)
        ]
        summary = " and ".join(pieces) if pieces else "[dim]no channels[/dim]"
        log.write(f" [dim]●[/dim] [bold]Channel hopping[/bold] across {summary}")
        if dropped:
            noun = "AP" if dropped == 1 else "APs"
            log.write(
                treelog.leaf(f"[dim]Cleared [bold]{dropped}[/bold] "
                             f"{noun} outside the filter[/dim]")
            )

    # ----- Filter bar --------------------------------------------------------

    def on_filter_bar_scan_filter_changed(self, message: FilterBar.ScanFilterChanged) -> None:
        self._scan_filter = message.scan_filter
        self.refresh_table()

    def on_filter_bar_edit_channels(self) -> None:
        self.action_change_channel()

    def _prune_aps_outside(self, channels: List[int]) -> int:
        array = self.app.array
        if not array:
            return 0
        keep = set(channels)
        stale = [
            bssid
            for bssid, ap in array.access_points.items()
            if ap.channel not in keep
        ]
        for bssid in stale:
            self._forget_row(bssid, drop_from_array=True)
        return len(stale)

    async def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        bssid = event.row_key.value
        target_ap = self.ap_cache.get(bssid)
        if target_ap:
            if self.app.array:
                await self.app.array.stop_hopping()
            self.app.target_ap = target_ap
            self.app.push_screen("focus")

    def on_data_table_header_selected(
        self, event: DataTable.HeaderSelected
    ) -> None:
        """Click a column header to sort by it; click again to flip direction."""
        key = event.column_key.value
        for idx, (col_key, _) in enumerate(self._COLUMNS):
            if col_key != key:
                continue
            if idx == self._sort_idx:
                self._sort_reverse = not self._sort_reverse
            else:
                self._sort_idx = idx
            Config.scanner_sort = self._COLUMNS[self._sort_idx][0]
            Config.scanner_sort_reverse = self._sort_reverse
            self.app.persist_config()
            self._update_column_headers()
            self._apply_sort(force=True)
            return
