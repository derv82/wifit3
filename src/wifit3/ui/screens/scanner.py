import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from textual.app import ComposeResult, RenderResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import Reactive
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, RichLog
from textual.widgets._header import HeaderClock, HeaderIcon, HeaderTitle
from rich.color import Color
from rich.markup import escape
from rich.style import Style
from rich.text import Span, Text

from wifit3.campaigns import treelog
from wifit3.campaigns.pbc import PbcWatcher, WpsPbcCapture
from wifit3.campaigns.wps.registrar import PinResult
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


FADE_DURATION_S = 30.0  # Seconds to fade a row after the AP does not see a beacon.
GRACE_DURATION_S = 7.0  # Time to wait after the last beacon before we begin fading a row.
MAX_FADE_FACTOR = 0.7
_FADE_STEPS = 10

SORT_INTERVAL_S = 2.0  # Table sort delay

# The 🥓 beacon counter increments ~10x/s per live AP. Stepping the *displayed*
# value that fast rewrote every row every frame and pinned the compositor (the
# dominant scanner CPU cost). Stepping it at most this often lets a steadily-
# beaconing row hold still between steps, so its table line stops re-rendering.
# The beacon-arrival flash still fires on the real count, so liveness is intact.
BEACON_DISPLAY_INTERVAL_S = 0.5


_ANSI_RGB = {
    "ansi_black": (0, 0, 0),
    "ansi_red": (128, 0, 0),
    "ansi_green": (0, 128, 0),
    "ansi_yellow": (128, 128, 0),
    "ansi_blue": (0, 0, 128),
    "ansi_magenta": (128, 0, 128),
    "ansi_cyan": (0, 128, 128),
    "ansi_white": (192, 192, 192),
    "ansi_bright_black": (128, 128, 128),
    "ansi_bright_red": (255, 0, 0),
    "ansi_bright_green": (0, 255, 0),
    "ansi_bright_yellow": (255, 255, 0),
    "ansi_bright_blue": (0, 0, 255),
    "ansi_bright_magenta": (255, 0, 255),
    "ansi_bright_cyan": (0, 255, 255),
    "ansi_bright_white": (255, 255, 255),
}
_ANSI_STYLE = {
    name: name.removeprefix("ansi_")
    for name in _ANSI_RGB
}
_ANSI_STYLE["ansi_default"] = ""
_ANSI_STYLE["transparent"] = ""


def _theme_rgb(value: str | None, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    if not value or value in ("transparent", "ansi_default"):
        return fallback
    if value in _ANSI_RGB:
        return _ANSI_RGB[value]
    h = value.lstrip("#")
    if len(h) == 6:
        try:
            return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except ValueError:
            pass
    return fallback


def _parse_style(style) -> Style:
    if isinstance(style, Style):
        return style
    if not style:
        return Style()
    tokens = [_ANSI_STYLE.get(tok, tok) for tok in str(style).split()]
    cleaned = " ".join(tok for tok in tokens if tok)
    try:
        return Style.parse(cleaned) if cleaned else Style()
    except Exception:
        return Style()


def _fade_text(text: Text, factor: float, bg: tuple[int, int, int]) -> Text:
    """Blend every span's foreground toward `bg` by `factor` (0..1)."""
    if factor <= 0:
        return text

    def _fade(style):
        parsed = _parse_style(style)
        if parsed.color is None:
            return parsed
        t = parsed.color.get_truecolor()
        s = 1.0 - factor
        return parsed + Style(color=Color.from_rgb(
            t.red * s + bg[0] * factor,
            t.green * s + bg[1] * factor,
            t.blue * s + bg[2] * factor,
        ))

    out = text.copy()
    out.style = _fade(out.style)
    out.spans = [Span(sp.start, sp.end, _fade(sp.style)) for sp in out.spans]
    return out


def _cells_key(cells: List[Text]) -> tuple:
    """A cheap, comparable fingerprint of a row's pre-fade cells: plain text plus
    styles (base + spans)."""
    return tuple(
        (c.plain, str(c.style), tuple((s.start, s.end, str(s.style)) for s in c.spans))
        for c in cells
    )


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
        Binding("home", "scroll_home", "Top", show=False, priority=True),
        Binding("end", "scroll_end", "Bottom", show=False, priority=True),
    ]

    # (column_key, display_label). Order here = on-screen order.
    _COLUMNS = [
        ("bssid", "BSSID"),
        ("channel", "CH"),
        ("signal", "POWER"),
        ("beacons", "🥓"),
        ("clients", "💻"),
        ("encryption", "ENCRYPT"),
        ("wps", "WPS"),
        ("ssid", "SSID"),
    ]

    # Columns whose values are right-aligned numerics.
    _RIGHT_ALIGNED = {"channel", "signal", "beacons", "clients"}

    # How long to flash the 🥓 cell when a beacon arrives.
    BEACON_FLASH_S = 0.2

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
        # Per-BSSID last render key (fade bucket + cell content).
        self._render_key: Dict[str, tuple] = {}
        # captures/ history, loaded once at mount and hydrated onto APs by
        # BSSID so previously-saved handshakes/PMKIDs/WEP keys re-badge.
        self._capture_index: Dict[str, List[PersistedCapture]] = {}
        # WPS PBC auto-invade. ON by default. The enabled flag lives on the app
        # (app.pbc_enabled). Watcher + capturing serialization stay Scanner-local.
        self._pbc_watcher = PbcWatcher()
        self._pbc_capturing = False          # serialize: one invade at a time

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
        self._sort_idx = next(
            (i for i, (key, _label) in enumerate(self._COLUMNS) if key == Config.scanner_sort), 2)
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
        return "Existing [bold]captures/[/bold]: " + ", ".join(parts)

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
        tv = self.app.theme_variables
        # Fade toward $surface (actual bg), not $background (the screen bg). Some Textual
        # themes use symbolic tokens (``transparent`` / ``ansi_default``), so fall back safely.
        bg_fallback = _theme_rgb(tv.get("background"), (0, 0, 0))
        bg = _theme_rgb(tv.get("surface"), bg_fallback)
        self._theme_fg = tv.get("foreground", "#ffffff")

        fade_span = max(0.001, FADE_DURATION_S - GRACE_DURATION_S)

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

            age = now - ap.last_seen
            if age >= FADE_DURATION_S:
                continue

            if not ap.persisted:
                hist = self._capture_index.get(ap.bssid)
                if hist:
                    ap.persisted = hist

            n_cli = client_counts.get(ap.bssid, 0)
            if age <= GRACE_DURATION_S:
                factor = 0.0
            else:
                prog = min(1.0, (age - GRACE_DURATION_S) / fade_span)
                factor = round(prog * _FADE_STEPS) / _FADE_STEPS * MAX_FADE_FACTOR

            # Beacon-arrival flash: bump the deadline when beacon count changes.
            prev = self._prev_beacons.get(ap.bssid)
            if prev is not None and ap.beacons > prev:
                self._beacon_flash_until[ap.bssid] = now + self.BEACON_FLASH_S
            self._prev_beacons[ap.bssid] = ap.beacons
            flash_bacon = now < self._beacon_flash_until.get(ap.bssid, 0.0)

            shown_beacons = self._display_beacons(ap, now)
            raw = self._build_cells(
                ap, n_cli, flash_bacon=flash_bacon, beacons_display=shown_beacons
            )
            # Render key = fade bucket + bg + pre-fade cell content.
            render_key = (factor, bg, _cells_key(raw))

            if ap.bssid not in self.ap_cache:
                self.ap_cache[ap.bssid] = ap
                self._render_key[ap.bssid] = render_key
                table.add_row(*(_fade_text(c, factor, bg) for c in raw), key=ap.bssid)
            else:
                # Decloak event: already logged here.
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
                if self._render_key.get(ap.bssid) != render_key:
                    self._render_key[ap.bssid] = render_key
                    cells = [_fade_text(c, factor, bg) for c in raw]
                    for (col_key, _), cell in zip(self._COLUMNS, cells):
                        table.update_cell(ap.bssid, col_key, cell)

            self._drain_capture_events(ap, array.forged_macs)

    def _apply_sort_and_evict(self) -> None:
        """Re-sort the table and drop fully-faded APs. Runs every 2 s."""
        self._evict_expired_aps()
        self._apply_sort(scroll_to_cursor=False)

    def _evict_expired_aps(self) -> None:
        if not self.app.array:
            return
        now = time.time()
        to_drop = [
            bssid for bssid, ap in self.ap_cache.items()
            if (now - ap.last_seen) >= FADE_DURATION_S
        ]
        for bssid in to_drop:
            self._forget_row(bssid, drop_from_array=True)

    def _forget_row(self, bssid: str, *, drop_from_array: bool) -> None:
        """Drop the AP's row and caches; drop_from_array also evicts it from the registry."""
        if drop_from_array and self.app.array:
            self.app.array.access_points.pop(bssid, None)
        self.ap_cache.pop(bssid, None)
        self._prev_beacons.pop(bssid, None)
        self._beacon_flash_until.pop(bssid, None)
        self._beacon_shown.pop(bssid, None)
        self._render_key.pop(bssid, None)
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

    def _build_cells(
        self, ap: AccessPoint, n_clients: int, flash_bacon: bool = False,
        beacons_display: Optional[int] = None,
    ) -> List[Text]:
        """Build the per-column full-color Text cells for one AP row."""
        fg = self._theme_fg
        bacon_style = f"{fg} bold" if flash_bacon else fg
        beacons = ap.beacons if beacons_display is None else beacons_display
        if ap.wps:
            wps_cell = Text("WPS 🔒" if ap.wps_locked else "WPS", style=fg)
        else:
            wps_cell = Text("", style=fg)
        return [
            Text(ap.bssid, style=fg),
            Text(str(ap.channel), justify="right", style=fg),
            Text(f"{ap.signal} dBm", justify="right", style=fg),
            Text(str(beacons), justify="right", style=bacon_style),
            Text(str(n_clients) if n_clients else "", justify="right", style=fg),
            # style=fg gives the bare '→' between WPA3/WPA2 a fadeable base color.
            Text.from_markup(format_encryption_markup(ap, muted=fg), emoji=False, style=fg),
            wps_cell,
            self._ssid_cell(ap),
        ]

    # Cap the SSID+badges cell so the trailing capture badges never overflow.
    _SSID_CELL_MAX = 32

    def _ssid_cell(self, ap: AccessPoint) -> Text:
        """name (bold=named, italic=hidden, +'?'=sibling guess) + chips."""
        if ap.ssid:
            name = Text(ap.ssid, style=f"{self._theme_fg} bold")
        else:
            sib = self._best_named_sibling_ssid(ap)
            name = Text(f"{sib}?" if sib else "<Hidden>", style=f"{self._theme_fg} italic")

        chips_markup = self._ssid_chips_markup(ap)  # Silenced, HS, PMK, WEP, PSK
        chips_text = Text.from_markup(chips_markup, emoji=False) if chips_markup else None
        reserved = 1 + chips_text.cell_len if chips_text else 0   # 1 = separator space
        name.truncate(max(1, self._SSID_CELL_MAX - reserved), overflow="ellipsis")
        if chips_text:
            name.append(" ")
            name.append_text(chips_text)
        return name

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
        """Badges next to SSID for HS, PMK, WEP, WPS."""
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

    def _apply_sort(self, *, scroll_to_cursor: bool = True) -> None:
        """Re-sort the table, maintains selected item.
        ``scroll_to_cursor`` controls whether the viewport follows the cursor."""
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
        # Only numeric columns try the int/float fast path.
        is_numeric_col = sort_key in self._RIGHT_ALIGNED

        def _key(val):
            if isinstance(val, Text):
                val = val.plain
            s = str(val).strip()
            is_empty = not s

            if is_empty:
                primary: object = 0 if is_numeric_col else ""
            elif is_numeric_col:
                # Strip non-numeric suffix (e.g. " dBm")
                head = s.split()[0]
                try:
                    primary = int(head)
                except ValueError:
                    try:
                        primary = float(head)
                    except ValueError:
                        # Numeric column with garbage content - sort last.
                        primary = float("inf") if not reverse else float("-inf")
            else:
                primary = s.lower()

            # Force empties to the bottom in BOTH sort directions.
            sentinel = int(is_empty != reverse)
            return (sentinel, primary)

        table.sort(sort_key, key=_key, reverse=reverse)

        if current_key:
            try:
                new_idx = table.get_row_index(current_key)
                if scroll_to_cursor:
                    table.move_cursor(row=new_idx, animate=False)
                else:
                    # Keep the highlight on the same AP across the reorder.
                    table.pin_cursor_row(new_idx)
            except Exception:
                pass

    # ----- Actions -----------------------------------------------------------

    def action_toggle_log(self) -> None:
        log_widget = self.query_one("#system-log")
        log_widget.display = not log_widget.display

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
        self._apply_sort()

    def action_toggle_sort_dir(self) -> None:
        self._sort_reverse = not self._sort_reverse
        Config.scanner_sort_reverse = self._sort_reverse
        self.app.persist_config()
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
            self._apply_sort()
            return
