import asyncio
import time
from pathlib import Path
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

from wifit3.engine.attacks import treelog
from wifit3.engine.attacks.wps.pbc import PbcWatcher, WpsPbcCapture
from wifit3.engine.attacks.wps.registrar import PinResult
from wifit3.engine.capture_history import load_capture_index, summarize
from wifit3.models import AccessPoint, PersistedCapture
from wifit3.engine.save import save_handshake, save_pmkid, save_wps_pbc

from ..capture_events import (
    CAPTURE_TOAST_TITLES, DECLOAK_METHOD_LABELS, CaptureEvent, CaptureEventDetector, CaptureKind,
)
from ..encryption_format import format_encryption_markup, wep_key_ascii
from wifit3.wlan.channels import band_label, band_ranges

from .channel_filter import ChannelFilterDialog


# Rows fade their foreground toward the DataTable's row background ($surface)
# over this duration, then get evicted on the next sort tick.
FADE_DURATION_S = 30.0

# Fresh rows stay at full brightness for this long before any fade starts —
# both as a "this AP is alive" signal and to absorb the inevitable 1-2s
# update jitter without the row starting to fade mid-conversation.
GRACE_DURATION_S = 7.0

# Cap the fade blend so stale rows stay readable — a full blend into the row
# bg leaves "black gaps" of unreadable text before eviction.
MAX_FADE_FACTOR = 0.7

# Quantize the fade into this many discrete brightness steps, so a row re-renders
# only when it crosses a step (~every 2 s) instead of on every 15 Hz tick — the
# render-key guard in refresh_table skips the unchanged ticks. [surprise-why]
_FADE_STEPS = 10

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


def _cells_key(cells: List[Text]) -> tuple:
    """A cheap, comparable fingerprint of a row's pre-fade cells — plain text plus
    styles (base + spans). Two ticks with the same fingerprint would render the row
    identically, so its repaint can be skipped."""
    return tuple(
        (c.plain, str(c.style), tuple((s.start, s.end, str(s.style)) for s in c.spans))
        for c in cells
    )


class _APScanTable(DataTable):
    """AP list table that can re-pin its row cursor without moving the viewport.

    The scanner re-sorts on a timer while the user is scrolling and navigating
    the list. Textual's DataTable scrolls the cursor back into view on *every*
    cursor-coordinate change — both ``move_cursor``'s own call and the
    ``cursor_coordinate`` watcher's (which also fires on ``move_cursor(scroll=
    False)``), and the watcher may *defer* that scroll to after the next refresh
    when the table's dimensions are mid-update. So a re-sort that shifts the
    selected row yanks the viewport, and a transient flag around ``move_cursor``
    can't catch the deferred case.

    ``_scroll_cursor_into_view`` is the single method all of those funnel
    through, so gating it here closes the deferred race too. ``pin_cursor_row``
    moves the cursor the normal way — so the highlight still tracks the AP via
    the framework — with only the scroll suppressed.
    """

    _suppress_scroll: bool = False

    def _scroll_cursor_into_view(self, animate: bool = False) -> None:
        if self._suppress_scroll:
            return
        super()._scroll_cursor_into_view(animate=animate)

    def pin_cursor_row(self, row: int) -> None:
        """Move the row cursor to ``row`` without scrolling the viewport."""
        self._suppress_scroll = True
        self.move_cursor(row=row, animate=False)
        # Reset only after the next refresh: the watcher's scroll-into-view can
        # be deferred via call_after_refresh, so a synchronous reset would fire
        # before it and the snap would leak back. Our reset is queued after the
        # deferred scroll, so that scroll still sees the flag set.
        self.call_after_refresh(self._release_scroll)

    def _release_scroll(self) -> None:
        self._suppress_scroll = False


class ScannerView(Screen):
    """The main AP scanning list screen."""

    BINDINGS = [
        Binding("q", "app.quit", "Quit", show=True),
        Binding("c", "change_channel", "Channel Filter", show=True),
        Binding("s", "cycle_sort", "Sort Col", show=True),
        Binding("o", "toggle_sort_dir", "Sort Asc/Desc", show=True),
        Binding("f", "toggle_fade", "Toggle Fade", show=True),
        Binding("l", "toggle_log", "Toggle Log", show=True),
        Binding("w", "wps_pbc_mode", "WPS PBC", show=True),
        Binding("home", "scroll_home", "Top", show=False, priority=True),
        Binding("end", "scroll_end", "Bottom", show=False, priority=True),
    ]

    # (column_key, display_label). Order here = on-screen order.
    # Headers are stored without the sort indicator; _update_column_headers adds a
    # 2-char sort marker (arrow trailing on text columns, leading on right-aligned
    # numeric ones) so column widths stay stable regardless of which is sorted.
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

    # Beacon-flash window — when ap.beacons increments, the 🥓 cell is
    # rendered bold for this many seconds. Long enough to be visible
    # (~3 refresh ticks at 15 Hz), short enough to not blur into a
    # continuous highlight on heavily-beaconing APs.
    BEACON_FLASH_S = 0.2

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
        # Per-BSSID prev-beacon-count + flash-deadline for "beacon arrived"
        # cell highlight. Cleared alongside ap_cache during eviction.
        self._prev_beacons: Dict[str, int] = {}
        self._beacon_flash_until: Dict[str, float] = {}
        # Per-BSSID last render key (fade bucket + cell content); skip a row's
        # repaint while unchanged. Cleared alongside ap_cache during eviction.
        self._render_key: Dict[str, tuple] = {}
        # Fade toggle (default on). When off: rows stay at full brightness
        # regardless of age, and the silent-AP eviction pass is skipped.
        self._fade_enabled: bool = True
        # captures/ history, loaded once at mount and hydrated onto APs by
        # BSSID so previously-saved handshakes/PMKIDs/WEP keys re-badge.
        self._capture_index: Dict[str, List[PersistedCapture]] = {}
        # WPS PBC auto-invade. ON by default: any AP that opens a push-button
        # window is auto-captured (unless we already hold its PSK). Press 'w' to
        # opt out. Detection is always-on + passive; only the invade transmits.
        # The enabled flag lives on the app (app.pbc_enabled) so Focus's 'w' toggles
        # the same setting. Watcher + capturing serialization stay Scanner-local.
        self._pbc_watcher = PbcWatcher()
        self._pbc_capturing = False          # serialize: one invade at a time

    # ----- Compose / mount ---------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            table = _APScanTable(cursor_type="row", id="ap-table")
            # Reserve 2 chars in every header so DataTable's auto-width
            # accounts for the sort indicator from creation — otherwise
            # narrow columns (e.g. "🥓") get clipped when sorted.
            for key, label in self._COLUMNS:
                table.add_column(label + "  ", key=key)
            yield table
            yield RichLog(id="system-log", markup=True, highlight=True)
        yield Footer()

    async def on_mount(self) -> None:
        log = self.query_one("#system-log", RichLog)
        self._update_column_headers()
        iface = self.app.active_interface

        # "Scanner initialized" group: a ● header, then ├─/└─ children. The last
        # child is the └ leaf, so collect them first and connect by position.
        log.write(treelog.header("Scanner initialized"))
        rows: List[str] = []
        summary = self._load_capture_history()
        if summary:
            rows.append(summary)
        if iface:
            hopped = self._channel_filter or list(iface.supported_channels)
            rows.append(
                "Hopping [italic]all available channels[/italic] "
                f"[bold cyan]{band_label(hopped)}[/bold cyan]"
            )
        else:
            rows.append("[yellow]No active interface[/yellow]")
        for i, row in enumerate(rows):
            log.write(treelog.leaf(row) if i == len(rows) - 1 else treelog.branch(row))

        if iface:
            # 15 FPS in-place value updates — no resort. Beacons arrive ~10 Hz
            # per AP at best, so 15 Hz is plenty and 4x cheaper than 60.
            self._refresh_timer = self.set_interval(1 / 15, self.refresh_table)
            # Lazy resort + evict expired APs.
            self._sort_timer = self.set_interval(
                SORT_INTERVAL_S, self._apply_sort_and_evict
            )
            # Passive PBC-window watch (1 Hz is plenty for a ~120s walk window).
            self._pbc_timer = self.set_interval(1.0, self._poll_pbc)
            # Auto-invade is ON by default — announce it so the active TX is
            # never a surprise; 'w' opts out (see action_wps_pbc_mode).
            self._log_pbc_status()

    def _load_capture_history(self) -> Optional[str]:
        """Load captures/ once; return the one-line summary (None if empty)."""
        self._capture_index = load_capture_index()
        return self._format_history_summary(*summarize(self._capture_index))

    @staticmethod
    def _format_history_summary(hs: int, pmkid: int, wep: int, wps: int) -> Optional[str]:
        """`Existing captures/: N handshakes, N PMKIDs, N WEP keys, N WPS PSKs` —
        counts are per-AP (see summarize); zero categories omitted; None when
        nothing."""
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
        return "[dim]Existing [bold]captures/[/bold]: " + ", ".join(parts) + "[/dim]"

    async def on_screen_resume(self) -> None:
        # Owns hopper restart so focus/dialog children don't have to know
        # about _channel_filter. start_hopping is idempotent, so the dialog
        # callback (which has already restarted with a new filter) is a
        # no-op here; returning from focus (where the hopper was stopped)
        # restarts it with the saved filter.
        iface = self.app.active_interface
        if not iface:
            return
        await iface.start_hopping(
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
                # Right-justify so the label sits over the right-aligned
                # numbers (no gap), with the sort arrow floating left over
                # the empty space to the left of the digits.
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
        # Fade toward $surface (the actual DataTable row bg), not $background
        # (the screen bg). Crucial on themes where surface is lighter than
        # background — fading to black would crush text on the cursor row.
        bg = _hex_rgb(tv.get("surface", tv.get("background", "#000000")))
        # Cache resolved theme fg on self so _build_cells / _ssid_markup can
        # pick it up without threading params (refresh tick is the only caller).
        self._theme_fg = tv.get("foreground", "#ffffff")

        # Length of the actual fade phase (after the grace window).
        fade_span = max(0.001, FADE_DURATION_S - GRACE_DURATION_S)

        fade_enabled = self._fade_enabled

        for ap in iface.get_access_points():
            age = now - ap.last_seen
            if fade_enabled and age >= FADE_DURATION_S:
                # Eviction runs on the 2 s sort tick — don't drop mid-frame.
                continue

            # Hydrate saved capture history (once) so persisted badges render
            # from first sight. Cheap dict lookup; only matches add anything.
            if not ap.persisted:
                hist = self._capture_index.get(ap.bssid)
                if hist:
                    ap.persisted = hist

            n_cli = client_counts.get(ap.bssid, 0)
            # Grace window: stay at full brightness as a "this AP is alive"
            # signal. After that, linear fade toward bg over fade_span,
            # bottoming out at MAX_FADE_FACTOR so text stays readable.
            # When fade is toggled off, all rows render at full brightness.
            if not fade_enabled or age <= GRACE_DURATION_S:
                factor = 0.0
            else:
                # Quantize into _FADE_STEPS levels so the render key (below) is
                # stable between steps and the repaint is skipped on those ticks.
                prog = min(1.0, (age - GRACE_DURATION_S) / fade_span)
                factor = round(prog * _FADE_STEPS) / _FADE_STEPS * MAX_FADE_FACTOR

            # Beacon-arrival flash: bump the deadline whenever the count
            # increments since we last saw this AP. First-sight rows skip
            # the flash — the row is already at full brightness from the
            # grace window, no extra signal needed.
            prev = self._prev_beacons.get(ap.bssid)
            if prev is not None and ap.beacons > prev:
                self._beacon_flash_until[ap.bssid] = now + self.BEACON_FLASH_S
            self._prev_beacons[ap.bssid] = ap.beacons
            flash_bacon = now < self._beacon_flash_until.get(ap.bssid, 0.0)

            raw = self._build_cells(ap, n_cli, flash_bacon=flash_bacon)
            # Render key = fade bucket + bg + pre-fade cell content. An unchanged
            # key means the row would repaint identically, so skip both the fade
            # blend and the update_cell writes — this is what stops the fade
            # animation from re-rendering the table every 15 Hz tick. [surprise-why]
            render_key = (factor, bg, _cells_key(raw))

            if ap.bssid not in self.ap_cache:
                self.ap_cache[ap.bssid] = ap
                self._render_key[ap.bssid] = render_key
                table.add_row(*(_fade_text(c, factor, bg) for c in raw), key=ap.bssid)
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
                if self._render_key.get(ap.bssid) != render_key:
                    self._render_key[ap.bssid] = render_key
                    cells = [_fade_text(c, factor, bg) for c in raw]
                    for (col_key, _), cell in zip(self._COLUMNS, cells):
                        table.update_cell(ap.bssid, col_key, cell)

            # Drain new capture events for this AP into the log.
            self._drain_capture_events(ap, iface.forged_macs)

    def _apply_sort_and_evict(self) -> None:
        """Re-sort the table and drop fully-faded APs. Runs every 2 s.

        Pins the cursor to its AP across the reorder but does NOT scroll to it:
        if the user has wheel-scrolled away to read another part of the list,
        yanking the viewport back every 2 s makes the list unusable (you scroll
        up, the tick fires, it snaps back down). Explicit user sorts still
        recenter — see _apply_sort's scroll_to_cursor.
        """
        self._evict_expired_aps()
        self._apply_sort(scroll_to_cursor=False)

    def _evict_expired_aps(self) -> None:
        if not self.app.active_interface:
            return
        # When fade is toggled off the user has explicitly asked to keep
        # all sightings — never evict silently.
        if not self._fade_enabled:
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
            self._prev_beacons.pop(bssid, None)
            self._beacon_flash_until.pop(bssid, None)
            self._render_key.pop(bssid, None)
            try:
                table.remove_row(bssid)
            except Exception:
                pass

    # ----- Cell construction -------------------------------------------------

    def _build_cells(
        self, ap: AccessPoint, n_clients: int, flash_bacon: bool = False
    ) -> List[Text]:
        """Build the per-column full-color Text cells for one AP row.
        Aging is applied by the caller via `_fade_text`.

        Detail parens like `(PSK)` get theme-fg so they fade with the row
        rather than competing with row-age as a separate signal — the row
        fade is the AP's health indicator.

        ``flash_bacon`` bolds the 🥓 cell for one flash window when a
        beacon just arrived — positive "AP is alive" signal that
        complements the negative staleness signal of the row fade.
        """
        fg = self._theme_fg
        bacon_style = f"{fg} bold" if flash_bacon else fg
        # WPS cell: empty when absent; "WPS 🔒" when locked (PIN attacks
        # rate-limited → Reaver/Pixie won't progress). The version (1.0/2.0) is
        # omitted here — nearly everything is WPS 2.0, so the digit adds noise.
        if ap.wps:
            wps_cell = Text("WPS 🔒" if ap.wps_locked else "WPS", style=fg)
        else:
            wps_cell = Text("", style=fg)
        return [
            Text(ap.bssid, style=fg),
            Text(str(ap.channel), justify="right", style=fg),
            Text(f"{ap.signal} dBm", justify="right", style=fg),
            Text(str(ap.beacons), justify="right", style=bacon_style),
            Text(str(n_clients) if n_clients else "", justify="right", style=fg),
            # style=fg gives the bare '→' between WPA3/WPA2 a fadeable base color.
            Text.from_markup(format_encryption_markup(ap, muted=fg), emoji=False, style=fg),
            wps_cell,
            self._ssid_markup(ap),
        ]

    # Cap the SSID+badges cell so the trailing capture badges never spill off
    # the (last) column's right edge. Only over-long SSIDs that *have* badges
    # get truncated with '…'; the full name is always in Focus.
    _SSID_CELL_MAX = 32

    def _ssid_markup(self, ap: AccessPoint) -> Text:
        """Three SSID rendering states:
          - Confirmed       → bold     ("RealNetwork")
          - Hidden, no hint → italic   ("<Hidden>")
          - Hidden, guess   → italic + trailing '?'  ("SiblingName?")
        Italic carries the 'hidden' signal across both hidden states; the
        '?' marks 'guessed via sibling BSSID'. Note: must not use 'dim' on
        the guess state — the AP-fade pipeline owns dim/fade for stale
        rows, and stacking would corrupt the fade animation.
        """
        if ap.ssid:
            ssid_str, style = ap.ssid, f"{self._theme_fg} bold"
        else:
            sib_ssid = self._best_named_sibling_ssid(ap)
            if sib_ssid:
                ssid_str, style = f"{sib_ssid}?", f"{self._theme_fg} italic"
            else:
                ssid_str, style = "<Hidden>", f"{self._theme_fg} italic"

        markers_markup = self._capture_marker_markup(ap)
        if not markers_markup:
            return Text(ssid_str, style=style)

        markers_text = Text.from_markup(markers_markup, emoji=False)
        avail = self._SSID_CELL_MAX - 1 - markers_text.cell_len  # 1 = separator
        if avail >= 1 and len(ssid_str) > avail:
            ssid_str = ssid_str[: max(1, avail - 1)] + "…"
        text = Text(ssid_str, style=style)
        text.append(" ")
        text.append_text(markers_text)
        return text

    def _best_named_sibling_ssid(self, ap: AccessPoint) -> Optional[str]:
        """Pick the sibling SSID to display for a hidden AP. Returns the
        SSID of the highest-beacon-count non-hidden sibling, or None when
        no sibling has surfaced its SSID yet (every sibling also hidden)."""
        iface = self.app.active_interface
        if not iface or not ap.siblings:
            return None
        best_ssid: Optional[str] = None
        best_beacons = -1
        for sib_bssid in ap.siblings:
            sib_ap = iface.access_points.get(sib_bssid)
            if sib_ap and sib_ap.ssid and sib_ap.beacons > best_beacons:
                best_ssid = sib_ap.ssid
                best_beacons = sib_ap.beacons
        return best_ssid

    @staticmethod
    def _capture_marker_markup(ap: AccessPoint) -> str:
        """Rich-markup capture badges. A badge lights from a live capture OR
        from loaded captures/ history (ap.persisted) — so a previously-saved
        handshake/PMKID/WEP key re-badges across runs. Same green either way;
        the Focus summary tells the live-vs-history story."""
        kinds = {p.kind for p in ap.persisted}
        has_hs = kinds.__contains__("HS") or any(
            hs.is_complete for hs in ap.handshakes.values())
        has_pmk = "PMKID" in kinds or any(hs.pmkid for hs in ap.handshakes.values())
        has_wep = "WEP" in kinds or ap.wep_key is not None
        has_wps = "WPS" in kinds or ap.wps_pbc_psk is not None
        parts: List[str] = []
        if has_hs:
            parts.append("[green]✓HS[/green]")
        if has_pmk:
            parts.append("[green]✓PMK[/green]")
        if has_wep:
            parts.append("[green]✓WEP[/green]")
        if has_wps:
            parts.append("[green]✓WPS[/green]")
        return " ".join(parts)

    # ----- Capture-event logging ---------------------------------------------

    def _drain_capture_events(self, ap: AccessPoint, forged_macs) -> None:
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
                f"[bold]{ap_label}[/bold] from [bold]{client}[/bold]"
            )
            save_result = save_handshake(ap, ev.client_mac)
        elif ev.kind == CaptureKind.UNCRACKABLE_HANDSHAKE:
            msg = (
                f"[bold yellow]● {escape(ev.value or '?')} 4-way[/bold yellow] on "
                f"[bold]{ap_label}[/bold] [dim]— not crackable (-m 22000)[/dim]"
            )
        elif ev.kind == CaptureKind.PMKID:
            msg = (
                f"[bold green]✓ PMKID[/bold green] on "
                f"[bold]{ap_label}[/bold] from [bold]{client}[/bold]"
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
                   f"[bold]{ap_label}[/bold] = {escape(wep_key_ascii(ev.value or ''))}")
        elif ev.kind == CaptureKind.WPS_PIN:
            msg = (f"[bold green]✓ WPS PIN[/bold green] on "
                   f"[bold]{ap_label}[/bold] = {escape(ev.value or '')}")
        elif ev.kind == CaptureKind.WPS_PSK:
            msg = (f'[bold green]✓ WPS PSK[/bold green] on '
                   f'[bold]{ap_label}[/bold] = "{escape(ev.value or "")}"')
        elif ev.kind == CaptureKind.WPS_PBC:
            msg = (f'[bold green]✓ WPS PSK[/bold green] [dim](via PushButton)[/dim] on '
                   f'[bold]{ap_label}[/bold] = "{escape(ev.value or "")}"')
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
                body = f"{name}: {wep_key_ascii(ev.value or '')}"
            elif ev.pair_label:
                body = f"{name} ({ev.pair_label})"
            else:
                body = name
            self.notify(body, title=title, timeout=6)

    def _write_log(self, text) -> None:
        try:
            log = self.query_one("#system-log", RichLog)
        except Exception:
            return
        # RichLog's emoji shortcode expansion is on by default — it'll happily
        # turn :ab: / :cd: inside a BSSID into 🆎 / 💿 (Unicode regional /
        # optical-disc emoji). Disable it whenever we hand in a markup string.
        # Pre-built Text objects (from _log_capture_event) come in already-
        # rendered, so they pass through unchanged.
        if isinstance(text, str):
            text = Text.from_markup(text, emoji=False)
        log.write(text)

    # ----- Sort --------------------------------------------------------------

    def _apply_sort(self, *, scroll_to_cursor: bool = True) -> None:
        """Re-sort the table, keeping the cursor on the same AP across the
        reorder. ``scroll_to_cursor`` controls whether the viewport follows the
        cursor: True for explicit user-triggered sorts (you acted, you expect to
        see the result), False for the periodic auto-sort (don't yank a viewport
        the user has scrolled away from)."""
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
        # Only numeric columns try the int/float fast path. For text columns
        # (SSID, BSSID, ENCRYPT) we ALWAYS use the lowercased string, so a
        # cell like "7" (a real decloaked single-char SSID) stays a string
        # and doesn't mix int with neighbour cells like "MyNetwork" —
        # Python rejects that comparison with a TypeError mid-sort.
        is_numeric_col = sort_key in self._RIGHT_ALIGNED

        def _key(val):
            if isinstance(val, Text):
                val = val.plain
            s = str(val).strip()
            is_empty = not s

            if is_empty:
                # Match the column's primary type so empties compare cleanly against
                # populated cells (mixing 0 with "foo" raises TypeError mid-sort).
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
                        # Numeric column with garbage content — sort it last
                        # rather than crashing. Shouldn't happen for the
                        # current set of numeric columns.
                        primary = float("inf") if not reverse else float("-inf")
            else:
                primary = s.lower()

            # Force empties to the bottom in BOTH sort directions. table.sort
            # sorts ascending by key then reverses if reverse=True, so the
            # "bottom" of the final list = the FIRST element after the sort.
            #   ascending  (reverse=False): empties want LARGEST  → sentinel 1
            #   descending (reverse=True):  empties want SMALLEST → sentinel 0
            # Reduces to: sentinel = 1 iff is_empty XOR reverse.
            sentinel = int(is_empty != reverse)
            return (sentinel, primary)

        table.sort(sort_key, key=_key, reverse=reverse)

        if current_key:
            try:
                new_idx = table.get_row_index(current_key)
                if scroll_to_cursor:
                    table.move_cursor(row=new_idx, animate=False)
                else:
                    # Keep the highlight on the same AP across the reorder
                    # without yanking the viewport the user scrolled to — the
                    # framework still moves the highlight, only the scroll is
                    # suppressed (see _APScanTable.pin_cursor_row).
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
        """React to PBC windows that are *already* open at the instant we arm.
        PbcWatcher consumed their False->True edge while we were OFF, so
        _poll_pbc won't re-fire them — without this, pressing 'w' mid-window does
        nothing (and says nothing) until the window closes and re-opens. For each
        open window: announce the ones we already own (so the press isn't a
        silent no-op), and invade the first one we don't (one-at-a-time; the poll
        loop catches any later edges)."""
        iface = self.app.active_interface
        if not iface:
            return
        launched = self._pbc_capturing
        for ap in iface.get_access_points():
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
                "[bold]WPS PushButton auto-invade[/bold] is "
                "[bold green]enabled[/bold green] [dim](press [bold]w[/bold] to toggle)[/dim]"))
            self._write_log(treelog.leaf(
                "[dim](automatically retrieves PSK when [bold italic]any[/bold italic] "
                "WPS button is pressed)[/dim]"))
        else:
            self._write_log(treelog.header(
                "[bold]WPS PushButton auto-invade[/bold] is "
                "[yellow]disabled[/yellow] [dim](press [bold]w[/bold] to toggle)[/dim]"))
            self._write_log(treelog.leaf(
                "[dim](detect + alert only — never transmits)[/dim]"))

    def _poll_pbc(self) -> None:
        # This 1 Hz timer keeps firing while Focus is pushed on top (Textual doesn't
        # pause a suspended screen's timers), and Focus runs its own PBC capture — so
        # a Scanner invade here would race Focus over the single radio. Bail unless
        # we're the *true* top screen. NOT is_current: that's also True for background
        # screens (the suspended Scanner under Focus is one), so it can't tell
        # foreground from suspended — use screen-stack identity.
        iface = self.app.active_interface
        if not iface or self.app.screen is not self:
            return
        for ap in self._pbc_watcher.new_windows(iface.get_access_points()):
            self._on_pbc_window(ap)

    def _on_pbc_window(self, ap: AccessPoint) -> None:
        label = escape(ap.ssid or ap.bssid)
        # Header (tree root) for this window.
        self._write_log(
            f"[bold cyan]WPS PushButton [italic]auto-invade:[/italic][/bold cyan] "
            f"[bold green]Open Window[/bold green] on [bold]{label}[/bold] "
            f"[dim](CH {ap.channel})[/dim]")
        if not self.app.pbc_enabled:
            self._write_log(treelog.leaf("[dim]auto-invade off — press [bold]w[/bold] to enable[/dim]"))
            return
        if ap.has_psk:
            wps = next((p for p in ap.persisted if p.kind == "WPS" and p.value), None)
            where = f" [dim]({escape(Path(wps.path).name)})[/dim]" if wps else ""
            self._write_log(treelog.leaf(f"[italic]already captured[/italic]{where}"))
            return
        if self._pbc_capturing:
            return
        asyncio.create_task(self._invade_pbc(ap))

    async def _invade_pbc(self, ap: AccessPoint) -> None:
        """Pause hop → tune to the target → run the PBC enrollment → resume.

        Engine sub-steps render as ├─► tree branches under the window header;
        the recovered PSK / failure closes the group with a └─ leaf.
        """
        iface = self.app.active_interface
        if not iface:
            return
        self._pbc_capturing = True
        label = escape(ap.ssid or ap.bssid)
        self._write_log(treelog.branch(
            f"[cyan]invading[/cyan] [bold]{label}[/bold] — pausing hop, "
            f"tuning [cyan]CH {ap.channel}[/cyan]…"))
        try:
            await iface.stop_hopping()
            await iface.set_channel(ap.channel)
            outcome = await WpsPbcCapture(
                iface, ap, log=lambda m: self._write_log(treelog.branch(m))
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
            # Resume hopping only if we're still the foreground screen (screen-stack
            # identity, not is_current — that's True for background screens too). If
            # the user switched to Focus mid-invade, Focus owns the channel and its
            # on_screen_resume restarts the hopper.
            if self.app.screen is self:
                await iface.start_hopping(channels=self._channel_filter, interval=0.25)

    def action_toggle_fade(self) -> None:
        self._fade_enabled = not self._fade_enabled
        log = self.query_one("#system-log", RichLog)
        if self._fade_enabled:
            log.write(
                f"[bold green][+] Fade ON[/bold green] "
                f"[dim](rows fade after {int(GRACE_DURATION_S)}s, "
                f"evict at {int(FADE_DURATION_S)}s)[/dim]"
            )
        else:
            log.write(
                "[bold yellow][+] Fade OFF[/bold yellow] "
                "[dim](rows stay full-bright, no eviction)[/dim]"
            )

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

        supported = iface.supported_channels
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

        pieces = [
            f"[bold cyan]{name}[/bold cyan] [dim]({rngs})[/dim]"
            for name, rngs in band_ranges(result)
        ]
        summary = " and ".join(pieces) if pieces else "[dim]no channels[/dim]"
        log.write(f"[bold]Channel hopping[/bold] across {summary}")
        if dropped:
            log.write(
                treelog.leaf(f"[dim]Cleared {dropped} AP(s) outside the filter[/dim]")
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
            self._prev_beacons.pop(bssid, None)
            self._beacon_flash_until.pop(bssid, None)
            self._render_key.pop(bssid, None)
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

    def on_data_table_header_selected(
        self, event: DataTable.HeaderSelected
    ) -> None:
        """Click a column header to sort by it; click again to flip direction.

        Mirrors the keyboard model: switching columns keeps the current
        direction (like the 's' binding), re-clicking the active column
        toggles asc/desc (like the 'o' binding).
        """
        key = event.column_key.value
        for idx, (col_key, _) in enumerate(self._COLUMNS):
            if col_key != key:
                continue
            if idx == self._sort_idx:
                self._sort_reverse = not self._sort_reverse
            else:
                self._sort_idx = idx
            self._update_column_headers()
            self._apply_sort()
            return
