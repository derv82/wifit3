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

from wifit3.engine.capture_history import load_capture_index, summarize
from wifit3.engine.models import AccessPoint, PersistedCapture

from ..capture_events import DECLOAK_METHOD_LABELS, CaptureEvent, CaptureEventDetector
from ..encryption_format import format_encryption_markup
from .channel_filter import ChannelFilterDialog
from .decloak_test_dialog import DecloakSsidDialog


# Rows fade their foreground toward the DataTable's row background ($surface)
# over this duration, then get evicted on the next sort tick.
FADE_DURATION_S = 30.0

# Fresh rows stay at full brightness for this long before any fade starts —
# both as a "this AP is alive" signal and to absorb the inevitable 1-2s
# update jitter without the row starting to fade mid-conversation.
GRACE_DURATION_S = 7.0

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
        Binding("d", "decloak", "Decloak Sel", show=True),
        Binding("D", "decloak_test", "Decloak Test", show=True),
        Binding("f", "toggle_fade", "Toggle Fade", show=True),
        Binding("l", "toggle_log", "Toggle Log", show=True),
        Binding("home", "scroll_home", "Top", show=False, priority=True),
        Binding("end", "scroll_end", "Bottom", show=False, priority=True),
    ]

    # (column_key, display_label). Order here = on-screen order.
    # Headers are stored without the sort-indicator suffix; _update_column_headers
    # always appends 2 chars (" ▼", " ▲", or "  ") so column widths stay stable
    # regardless of which column is currently sorted.
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
        # Guards re-entry into action_decloak. Set/cleared in the background
        # task so the action handler can return immediately.
        self._decloak_in_progress = False
        # Capture-event detector — coarse (no per-EAPOL spam in the scanner).
        self._events = CaptureEventDetector(granular_eapol=False)
        # Per-BSSID prev-beacon-count + flash-deadline for "beacon arrived"
        # cell highlight. Cleared alongside ap_cache during eviction.
        self._prev_beacons: Dict[str, int] = {}
        self._beacon_flash_until: Dict[str, float] = {}
        # Fade toggle (default on). When off: rows stay at full brightness
        # regardless of age, and the silent-AP eviction pass is skipped.
        self._fade_enabled: bool = True
        # captures/ history, loaded once at mount and hydrated onto APs by
        # BSSID so previously-saved handshakes/PMKIDs/WEP keys re-badge.
        self._capture_index: Dict[str, List[PersistedCapture]] = {}

    # ----- Compose / mount ---------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            table = DataTable(cursor_type="row", id="ap-table")
            # Reserve 2 chars in every header so DataTable's auto-width
            # accounts for the sort indicator from creation — otherwise
            # narrow columns (e.g. "🥓s") get clipped when sorted.
            for key, label in self._COLUMNS:
                table.add_column(label + "  ", key=key)
            yield table
            yield RichLog(id="system-log", markup=True, highlight=True)
        yield Footer()

    async def on_mount(self) -> None:
        log = self.query_one("#system-log", RichLog)
        log.write("[bold green]Scanner Initialized.[/bold green]")
        log.write(
            f"[dim]Rows stay bright for {int(GRACE_DURATION_S)}s after a beacon, "
            f"then fade out over {int(FADE_DURATION_S - GRACE_DURATION_S)}s of silence "
            f"and disappear. Press [bold]f[/bold] to toggle fading.[/dim]"
        )
        self._load_capture_history()
        self._update_column_headers()

        if self.app.active_interface:
            log.write(
                f"[cyan]Starting channel hopper on "
                f"{self.app.active_interface.name}...[/cyan]"
            )
            # 15 FPS in-place value updates — no resort. Beacons arrive ~10 Hz
            # per AP at best, so 15 Hz is plenty and 4x cheaper than 60.
            self._refresh_timer = self.set_interval(1 / 15, self.refresh_table)
            # Lazy resort + evict expired APs.
            self._sort_timer = self.set_interval(
                SORT_INTERVAL_S, self._apply_sort_and_evict
            )

    def _load_capture_history(self) -> None:
        """Load captures/ once and log a one-line headline. Silent if empty."""
        self._capture_index = load_capture_index()
        summary = self._format_history_summary(*summarize(self._capture_index))
        if summary:
            self._write_log(summary)

    @staticmethod
    def _format_history_summary(hs: int, pmkid: int, wep: int) -> Optional[str]:
        """`Found in captures/: N handshakes, N PMKIDs, N WEP keys` — counts are
        per-AP (see summarize); zero categories omitted; None when nothing."""
        parts = []
        if hs:
            parts.append(f"[green bold]{hs} handshake{'s' * (hs != 1)}[/green bold]")
        if pmkid:
            parts.append(f"[green bold]{pmkid} PMKID{'s' * (pmkid != 1)}[/green bold]")
        if wep:
            parts.append(f"[green bold]{wep} WEP key{'s' * (wep != 1)}[/green bold]")
        if not parts:
            return None
        return "[bold]Found in[/] [cyan bold]captures/[/]: " + ", ".join(parts)

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
            # signal. After that, linear fade to bg over fade_span.
            # When fade is toggled off, all rows render at full brightness.
            if not fade_enabled or age <= GRACE_DURATION_S:
                factor = 0.0
            else:
                factor = min(1.0, (age - GRACE_DURATION_S) / fade_span)

            # Beacon-arrival flash: bump the deadline whenever the count
            # increments since we last saw this AP. First-sight rows skip
            # the flash — the row is already at full brightness from the
            # grace window, no extra signal needed.
            prev = self._prev_beacons.get(ap.bssid)
            if prev is not None and ap.beacons > prev:
                self._beacon_flash_until[ap.bssid] = now + self.BEACON_FLASH_S
            self._prev_beacons[ap.bssid] = ap.beacons
            flash_bacon = now < self._beacon_flash_until.get(ap.bssid, 0.0)

            cells = [
                _fade_text(c, factor, bg)
                for c in self._build_cells(ap, n_cli, flash_bacon=flash_bacon)
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
        # rate-limited → Reaver/Pixie won't progress). Version (1.0/2.0) is
        # kept on the model for the Focus panel / Pixie targeting but omitted
        # here — nearly everything is WPS 2.0, so the digit was just noise.
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
        parts: List[str] = []
        if has_hs:
            parts.append("[green]✓HS[/green]")
        if has_pmk:
            parts.append("[green]✓PMK[/green]")
        if has_wep:
            parts.append("[green]✓WEP[/green]")
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
        elif ev.kind == "decloak":
            method_label = DECLOAK_METHOD_LABELS.get(ev.method or "", ev.method or "?")
            msg = (
                f"[bold]Decloaked[/bold] [cyan]{escape(ev.bssid)}[/cyan] → "
                f"[green]{escape(ev.ssid or '')}[/green] "
                f"[dim]via {method_label}[/dim]"
            )
        else:
            return  # eapol events suppressed in scanner
        self._write_log(Text.from_markup(msg, emoji=False))

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
                # Match the column's primary type so empties compare cleanly
                # against populated cells. Mixing 0 with "foo" was the bug.
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
                table.move_cursor(row=new_idx, animate=False)
            except Exception:
                pass

    # ----- Actions -----------------------------------------------------------

    def action_toggle_log(self) -> None:
        log_widget = self.query_one("#system-log")
        log_widget.display = not log_widget.display

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

    def action_decloak(self) -> None:
        """Validate selection + spawn the actual attack as a background task,
        returning immediately so the TUI stays responsive (sort / scroll /
        other actions don't queue behind the 5-second sweep)."""
        if self._decloak_in_progress:
            self._write_log(
                "[yellow]Decloak already running. Wait for it to finish.[/yellow]"
            )
            return
        iface = self.app.active_interface
        if not iface:
            self._write_log("[bold red][!] No active interface.[/bold red]")
            return
        table = self.query_one("#ap-table", DataTable)
        try:
            bssid = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return
        if not bssid:
            return
        ap = iface.access_points.get(bssid)
        if not ap:
            return
        if ap.ssid:
            self._write_log(
                f"[yellow]'d' targets hidden APs — '{escape(ap.ssid)}' is already known.[/yellow]"
            )
            return
        base = self._best_named_sibling_ssid(ap)
        if not base:
            self._write_log(
                f"[yellow]No named sibling for {escape(bssid)} — nothing to guess from.[/yellow]"
            )
            return

        self._decloak_in_progress = True
        # Defer all the awaitable work into a background task — action_decloak
        # itself returns synchronously here so subsequent keypresses (sort,
        # scroll, even another 'd' which gets the busy message) don't queue
        # behind a ~5-second `await attack.run()`.
        import asyncio
        asyncio.create_task(self._run_decloak(iface, ap, base))

    async def _run_decloak(self, iface, ap, base: str) -> None:
        from wifit3.engine.attacks.decloak import DecloakAttack, build_candidates

        bssid = ap.bssid
        candidates = build_candidates(base)
        self._write_log(
            f"[cyan]Decloaking[/cyan] [bold]{escape(bssid)}[/bold] — "
            f"base [bold]{escape(base)}[/bold], {len(candidates)} candidates on CH {ap.channel}"
        )

        try:
            # Stop hopping so the channel stays locked while we wait for echoes.
            # Restore in the inner `finally` so a crashed attack still resumes
            # scanning — and preserves the current channel filter instead of
            # dumping the user back into the default all-channels sweep.
            await iface.stop_hopping()
            # Snapshot beacon count from the target so we can report at the
            # end whether RX was even hearing the AP during the sweep. If 0,
            # something's wrong with the channel/radio, not the candidate list.
            beacons_before = (
                iface.access_points[bssid].beacons
                if bssid in iface.access_points
                else 0
            )
            try:
                attack = DecloakAttack(iface, ap, base_ssid=base)
                result = await attack.run()
                ap_state = iface.access_points.get(bssid)
                beacons_heard = (ap_state.beacons - beacons_before) if ap_state else 0
                if result is None:
                    if beacons_heard == 0:
                        # Hard signal: RX wasn't even seeing the AP. Channel
                        # mistuned, radio busy, or the AP went silent.
                        self._write_log(
                            f"[bold red]Decloak failed[/bold red] for {escape(bssid)} — "
                            f"[bold]no beacons heard from target during sweep[/bold] "
                            f"(channel/radio issue, not a candidate-list issue)."
                        )
                    else:
                        self._write_log(
                            f"[yellow]Decloak exhausted[/yellow] for {escape(bssid)} — "
                            f"no candidate elicited a response "
                            f"([italic]{beacons_heard} beacons heard from target, "
                            f"RX is working — candidate list just didn't match[/italic])."
                        )
                # Success log is fired by the existing CaptureEventDetector
                # pipeline (it observes the SSID flip on the next poll).
            finally:
                await iface.start_hopping(
                    channels=self._channel_filter, interval=0.25
                )
        finally:
            self._decloak_in_progress = False

    def action_decloak_test(self) -> None:
        """Pipeline-verification mode: pops a dialog asking for SSID(s) and
        probes the selected AP with those exact strings (no sibling lookup,
        no suffix generation). Intended for testing against a router
        deliberately configured with a known hidden SSID."""
        if self._decloak_in_progress:
            self._write_log(
                "[yellow]Decloak already running. Wait for it to finish.[/yellow]"
            )
            return
        iface = self.app.active_interface
        if not iface:
            self._write_log("[bold red][!] No active interface.[/bold red]")
            return
        table = self.query_one("#ap-table", DataTable)
        try:
            bssid = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return
        if not bssid:
            return
        ap = iface.access_points.get(bssid)
        if not ap:
            return

        # Pre-fill: sibling SSID first (if any), then AP's own SSID (visible
        # APs being tested for TX), else empty.
        prefill = self._best_named_sibling_ssid(ap) or (ap.ssid or "")

        def _on_submit(ssids: Optional[List[str]]) -> None:
            if not ssids:
                self._write_log("[dim]Decloak test cancelled.[/dim]")
                return
            self._decloak_in_progress = True
            import asyncio
            asyncio.create_task(self._run_decloak_test(iface, ap, ssids))

        self.app.push_screen(DecloakSsidDialog(bssid, prefill), _on_submit)

    async def _run_decloak_test(
        self, iface, ap, ssids: List[str]
    ) -> None:
        from wifit3.engine.attacks.decloak import DecloakAttack

        bssid = ap.bssid
        self._write_log(
            f"[cyan]Decloak test[/cyan] [bold]{escape(bssid)}[/bold] — "
            f"{len(ssids)} explicit SSID(s) on CH {ap.channel}"
        )

        try:
            await iface.stop_hopping()
            beacons_before = (
                iface.access_points[bssid].beacons
                if bssid in iface.access_points
                else 0
            )
            try:
                attack = DecloakAttack(
                    iface, ap, base_ssid="", candidates_override=ssids
                )
                result = await attack.run()
                ap_state = iface.access_points.get(bssid)
                beacons_heard = (
                    (ap_state.beacons - beacons_before) if ap_state else 0
                )
                if result is None:
                    if beacons_heard == 0:
                        self._write_log(
                            f"[bold red]Test failed[/bold red] for {escape(bssid)} — "
                            f"[bold]no beacons heard from target during sweep[/bold] "
                            f"(channel/radio issue, not an SSID-list issue)."
                        )
                    else:
                        # For a HIDDEN target this means "no SSID matched". For
                        # an already-VISIBLE target the existing decloak path
                        # can't surface a "success" anyway (ap.ssid is already
                        # set, no transition to detect) — same message reads
                        # correctly either way.
                        self._write_log(
                            f"[yellow]Test exhausted[/yellow] for {escape(bssid)} — "
                            f"no SSID matched "
                            f"([italic]{beacons_heard} beacons heard, "
                            f"RX is working[/italic])."
                        )
                # Success: CaptureEventDetector fires the "Decloaked …" event
                # via the normal SSID-transition path. Nothing extra needed.
            finally:
                await iface.start_hopping(
                    channels=self._channel_filter, interval=0.25
                )
        finally:
            self._decloak_in_progress = False

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
            self._prev_beacons.pop(bssid, None)
            self._beacon_flash_until.pop(bssid, None)
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
