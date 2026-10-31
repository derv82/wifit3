import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from textual.app import ComposeResult, RenderResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import Reactive
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.timer import Timer
from textual.widgets import Footer, Header, RichLog, Static
from textual.widgets._header import HeaderClock, HeaderIcon, HeaderTitle
from rich.markup import escape
from rich.text import Text

from ..ap_table import APTable, APRow, COLUMN_KEYS
from ..selectable_rich_log import SelectableRichLog

from wifit3.campaigns import treelog
from wifit3.campaigns.pbc import PbcWatcher, WpsPbcCapture
from wifit3.campaigns.pin import EMPTY_PIN_LABEL
from wifit3.campaigns.wps.registrar import PinResult
from wifit3.persist.ap_state import PasswordStatus
from wifit3.persist.config import Config
from wifit3.models import AccessPoint
from wifit3.crack.handshake import pmkid_crackable
from wifit3.ui.vault.global_tracker import GlobalJobTracker
from wifit3.watchers.ap_watcher import (
    ApDecloaked, HandshakeCaptured, PmkidCaptured, UncrackableHandshakeSeen, WepKeyRecovered,
    WpsPbcPskRecovered, WpsPinRecovered, WpsPskRecovered,
)

from ..capture_log import DECLOAK_METHOD_LABELS, saved_psk_markup
from ..encryption_format import EncryptionSummary, wep_key_hex_and_ascii
from wifit3.wlan.channels import band_ranges

from .channel_filter import ChannelFilterDialog
from .filter import FilterBar, ScanFilter

if TYPE_CHECKING:
    from wifit3.ui.app import WifiteApp


STALE_DURATION_S = 10.0  # Seconds without a beacon before an AP row is dimmed.
EVICT_DURATION_S = 30.0  # Seconds without a beacon before an AP is dropped from the table.


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
        array = self.app.array
        members = array.members if array else []
        channels = " | ".join(f"CH:{m.current_channel:>3}" for m in members)
        if channels != self.channels:
            self.channels = channels
            self.parent.update_channel_layout(channels)

    def render(self) -> RenderResult:
        return Text(self.channels)


class _ChannelBar(Static):
    DEFAULT_CSS = """
    _ChannelBar {
        display: none;
        width: 100%;
        height: 1;
        background: $panel;
        tint: $foreground-darken-1 5%;
        color: $foreground;
        text-opacity: 85%;
        content-align: right middle;
    }
    """


class _ScannerHeader(Header):
    """Header whose right slot shows the hopped channel(s) in place of the clock."""

    _channel_layout_inputs: tuple = ()
    _use_channel_bar = False

    def compose(self) -> ComposeResult:
        yield HeaderIcon().data_bind(Header.icon)
        yield HeaderTitle()
        yield _ChannelReadout()

    def update_channel_layout(self, channels: str) -> None:
        title_width = self.format_title().cell_length
        inputs = (self.size.width, title_width, len(channels))
        if inputs != self._channel_layout_inputs:
            self._channel_layout_inputs = inputs
            title_right = (self.size.width + title_width + 1) // 2
            channels_left = self.size.width - len(channels)
            self._use_channel_bar = bool(channels) and channels_left - title_right < 3
            self.query_one(_ChannelReadout).display = not self._use_channel_bar
            for channel_bar in self.screen.query(_ChannelBar):
                channel_bar.display = self._use_channel_bar
        if self._use_channel_bar:
            for channel_bar in self.screen.query(_ChannelBar):
                channel_bar.update(channels)

    def on_resize(self) -> None:
        self.update_channel_layout(self.query_one(_ChannelReadout).channels)


class ScannerView(Screen):
    """The main AP scanning list screen."""

    app: "WifiteApp"

    HORIZONTAL_BREAKPOINTS = [
        (0, "-narrow"), (67, "-compact"), (71, "-medium"), (76, "-large"), (83, "-wide"),
    ]

    CSS = """
    ScannerView.-narrow #filter-encryption-label,
    ScannerView.-compact #filter-encryption-label { display: none; }
    ScannerView.-medium #filter-encryption-label,
    ScannerView.-large #filter-encryption-label { width: 3; }
    ScannerView.-narrow #filter-channels-label { display: none; }
    ScannerView.-compact #filter-channels-label,
    ScannerView.-medium #filter-channels-label { width: 2; }
    """

    BINDINGS = [
        Binding("q", "app.quit", "Quit", show=True),
        Binding("c", "change_channel", "Channel Filter", show=True),
        Binding("e", "focus_encryption", "Encryption", show=True),
        Binding("s", "cycle_sort", "Sort Col", show=True),
        Binding("o", "toggle_sort_dir", "Sort Asc/Desc", show=True),
        Binding("f", "focus_filter", "Filter", show=True),
        Binding("/", "focus_filter", "Filter", show=False),
        Binding("l", "toggle_log", "Toggle Log", show=True),
        Binding("w", "wps_pbc_mode", "WPS PBC", show=True),
        Binding("v", "open_vault", "Vault", show=True),
        Binding("home", "scroll_home", "Top", show=False, priority=True),
        Binding("end", "scroll_end", "Bottom", show=False, priority=True),
        Binding("g", "scroll_home", "Top", show=False, priority=True),
        Binding("G", "scroll_end", "Bottom", show=False, priority=True),
    ]

    # How long to flash the 🥓 cell when a beacon arrives.
    BEACON_FLASH_S = 0.2

    def __init__(self):
        super().__init__()
        self.ap_cache: Dict[str, AccessPoint] = {}
        self._refresh_timer: Optional[Timer] = None
        self._pbc_timer: Optional[Timer] = None
        self._last_sort_time: float = 0.0
        self._channel_filter: Optional[List[int]] = None
        self._scan_filter: ScanFilter = ScanFilter(hide_silenced=Config.hide_silenced)
        # Per-BSSID prev-beacon-count + flash-deadline for "beacon arrived"
        # cell highlight.
        self._prev_beacons: Dict[str, int] = {}
        self._beacon_flash_until: Dict[str, float] = {}
        # Per-BSSID (signature, row): an unchanged AP reuses its row object.
        self._row_cache: Dict[str, tuple] = {}
        # WPS PBC auto-invade. ON by default. The enabled flag lives on the app
        # (app.pbc_enabled). Watcher + capturing serialization stay Scanner-local.
        self._pbc_watcher = PbcWatcher()
        self._pbc_capturing = False          # serialize: one invade at a time
        self._logged_auto_crack: Optional[bool] = None

    # ----- Compose / mount ---------------------------------------------------

    def compose(self) -> ComposeResult:
        yield _ScannerHeader()
        yield _ChannelBar()
        array = self.app.array
        supported = list(array.supported_channels) if array else []
        with Vertical():
            yield FilterBar(supported)
            yield APTable(id="ap-table")
            yield SelectableRichLog(id="system-log", markup=True, highlight=True)
        yield GlobalJobTracker()
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#ap-table", APTable)
        stored = "identity" if Config.scanner_sort in ("vendor", "brand") else Config.scanner_sort
        table.sort_column = stored if stored in COLUMN_KEYS else "signal"
        table.sort_reverse = Config.scanner_sort_reverse
        table.focus()
        array = self.app.array

        self._write_log(treelog.header("Scanner initialized"))
        rows: List[str] = []
        summary = self.app.vault.summary()
        if summary:
            rows.append(f"Existing [bold]{Path(Config.captures_dir).as_posix()}/[/bold]: {summary}")
        if array:
            device_line = device_scan_summary(array.members)
            if device_line:
                rows.append(device_line)
        else:
            rows.append("[yellow]No active interface[/yellow]")
        for i, row in enumerate(rows):
            self._write_log(treelog.leaf(row) if i == len(rows) - 1 else treelog.branch(row))

        if array:
            # 15 FPS in-place value updates and sort refreshes.
            self._refresh_timer = self.set_interval(1 / 15, self.refresh_table)
            self._pbc_timer = self.set_interval(1.0, self._poll_pbc)
            self._log_pbc_status()  # Auto-invade is ON by default
            self._log_auto_crack_status_when_changed()

    async def on_screen_resume(self) -> None:
        # Restart channel hopper
        array = self.app.array
        if not array:
            return
        self._log_auto_crack_status_when_changed()
        await array.start_hopping(
            channels=self._channel_filter, interval=0.25
        )

    # ----- Per-tick refresh --------------------------------------------------

    def refresh_table(self) -> None:
        array = self.app.array
        if not array:
            return
        self._evict_expired_aps()
        # Under an opaque screen (Focus) the table is hidden: skip the rows. Textual's is_current is
        # True here, and every table repaint would repaint Focus too.
        visible = self.is_active or self.app.screen.styles.background.a < 1

        # Pre-compute per-AP client counts to avoid O(N×M) inside the AP loop below.
        client_counts: Dict[str, int] = {}
        for c in array.clients.values() if visible else ():
            if c.bssid and c.mac not in array.forged_macs:
                client_counts[c.bssid] = client_counts.get(c.bssid, 0) + 1

        now = time.time()
        rows: List[APRow] = []
        for ap in array.get_access_points(include_eviltwin=False):
            guessid = None
            if ap.ssid is None:
                guessid = Config.decloaked_ssid(ap.bssid) or array.guessid(ap) or None
            if not self._scan_filter.matches(ap, ssid=guessid):
                if ap.bssid in self.ap_cache:
                    self._forget_row(ap.bssid, drop_from_array=False)
                continue

            age = self._ap_row_age(ap, now)
            if age >= EVICT_DURATION_S:
                continue

            self.ap_cache[ap.bssid] = ap
            if visible:
                rows.append(self._row_for(ap, now, client_counts.get(ap.bssid, 0), guessid))

        if not visible:
            return
        table = self.query_one("#ap-table", APTable)
        table.set_rows(rows)
        if self._should_sort():
            self._last_sort_time = now
            table.resort()

    def _row_for(
        self, ap: AccessPoint, now: float, clients: int, guessid: Optional[str]
    ) -> APRow:
        """The AP as plain values; the table owns every colour decision made from them.
        An AP whose signature is unchanged reuses its previous row object untouched."""
        previous = self._prev_beacons.get(ap.bssid)
        if previous is not None and ap.beacons > previous:
            self._beacon_flash_until[ap.bssid] = now + self.BEACON_FLASH_S
        self._prev_beacons[ap.bssid] = ap.beacons

        vault = self.app.vault
        is_stale = self._ap_row_age(ap, now) > STALE_DURATION_S
        beacon_flash = now < self._beacon_flash_until.get(ap.bssid, 0.0)
        signature = self._row_signature(
            ap, vault, clients, is_stale, beacon_flash, guessid)
        # EAPOL rewrites ap.handshakes off the beacon path, so APs holding one are
        # never cached. There are only ever a handful of them.
        cacheable = not ap.handshakes
        if cacheable:
            cached = self._row_cache.get(ap.bssid)
            if cached is not None and cached[0] == signature:
                return cached[1]

        row = APRow(
            bssid=ap.bssid,
            ssid=ap.ssid,
            guessid=guessid,
            channel=ap.channel,
            signal=ap.signal,
            beacons=ap.beacons,
            clients=clients,
            encryption=EncryptionSummary.from_ap(ap),
            wps=ap.wps,
            wps_locked=ap.wps_locked,
            chipset=ap.chipset or "",
            identity=ap.identity.summary,
            is_stale=is_stale,
            beacon_flash=beacon_flash,
            silenced=Config.is_silenced(ap.bssid),
            has_handshake=vault.has_handshake(ap) or any(
                hs.is_complete for hs in ap.handshakes.values()),
            has_pmkid=vault.has_pmkid(ap) or any(
                hs.pmkid and pmkid_crackable(hs) for hs in ap.handshakes.values()),
            has_wep_key=vault.has_wep_key(ap) or ap.wep_key is not None,
            has_wps_psk=vault.has_wps_psk(ap) or ap.wps_pbc_psk is not None,
        )
        if cacheable:
            self._row_cache[ap.bssid] = (signature, row)
        return row

    def _row_signature(
        self, ap: AccessPoint, vault, clients: int, is_stale: bool,
        beacon_flash: bool, guessid: Optional[str],
    ) -> tuple:
        """Every input to the row, reduced to values that are cheap to read.

        ``last_seen`` stands in for the whole beacon/probe-response path: wlan/sink.py
        rewrites it after every IE-derived field, so encryption, AKMs, channel and the
        rest cannot move without it. Listed explicitly alongside it are the fields a
        frame can change WITHOUT touching last_seen -- cross-card RSSI, a decloak from a
        client's probe, a WPS M1's identity, WEP IV counting -- plus the vault and
        config state the badges read. tests/ui/test_scanner_row_cache.py holds the
        cached row against a freshly built one, so a missing input here fails loudly.
        """
        wep = ap.wep
        return (
            ap.last_seen, ap.beacons, ap.signal, ap.channel, ap.ssid,
            ap.wps, ap.wps_locked, ap.identity.summary,
            wep.unique_ivs if wep else 0, ap.wep_key, ap.wps_pbc_psk,
            clients, is_stale, beacon_flash, guessid,
            vault.revision(ap.bssid), Config.is_silenced(ap.bssid),
        )

    def _evict_expired_aps(self) -> None:
        if not self.app.array:
            return
        now = time.time()
        to_drop = [
            ap.bssid for ap in self.app.array.get_access_points(include_eviltwin=False)
            if self._ap_row_age(ap, now) >= EVICT_DURATION_S
        ]
        for bssid in to_drop:
            self._forget_row(bssid, drop_from_array=True)

    def _ap_row_age(self, ap: AccessPoint, now: float) -> float:
        return max(0.0, now - ap.last_seen)

    def _forget_row(self, bssid: str, *, drop_from_array: bool) -> None:
        """Drop the AP's row and caches; drop_from_array also evicts it and its clients from the registry."""
        if drop_from_array and self.app.array:
            self.app.array.remove_access_point(bssid)
        self.ap_cache.pop(bssid, None)
        self._prev_beacons.pop(bssid, None)
        self._beacon_flash_until.pop(bssid, None)
        self._row_cache.pop(bssid, None)

    # ----- Capture-event logging ---------------------------------------------

    def on_wifite_app_ap_event_detected(self, message: "WifiteApp.ApEventDetected") -> None:
        ev = message.event
        ap_label = escape(ev.ssid or ev.bssid)
        if isinstance(ev, HandshakeCaptured):
            msg = (
                f"[bold green]✓ HANDSHAKE[/bold green] ({ev.pair_label}) on "
                f"[bold cyan]{ap_label}[/bold cyan] from [bold]{escape(ev.client_mac)}[/bold]"
            )
        elif isinstance(ev, UncrackableHandshakeSeen):
            msg = (
                f"[bold yellow]● {escape(ev.akm_label)} 4-way[/bold yellow] on "
                f"[bold cyan]{ap_label}[/bold cyan] [dim](not crackable, -m 22000)[/dim]"
            )
        elif isinstance(ev, PmkidCaptured):
            msg = (
                f"[bold green]✓ PMKID[/bold green] on "
                f"[bold cyan]{ap_label}[/bold cyan] from [bold]{escape(ev.client_mac)}[/bold]"
            )
        elif isinstance(ev, ApDecloaked):
            # A ● header (not a ✓ win): a hidden SSID became visible, not a credential.
            method_label = DECLOAK_METHOD_LABELS.get(ev.method or "", ev.method or "?")
            self._write_log(Text.from_markup(treelog.header(
                f"[bold]Decloaked[/bold] [cyan]{escape(ev.bssid)}[/cyan] → "
                f"[green]{escape(ev.ssid or '')}[/green] "
                f"[dim]via {method_label}[/dim]"), emoji=False))
            return
        elif isinstance(ev, WepKeyRecovered):
            msg = (f"[bold green]✓ WEP KEY[/bold green] on "
                   f"[bold cyan]{ap_label}[/bold cyan] = {escape(wep_key_hex_and_ascii(ev.key_hex))}")
        elif isinstance(ev, WpsPinRecovered):
            msg = (f"[bold green]✓ WPS PIN[/bold green] on "
                   f"[bold cyan]{ap_label}[/bold cyan] = {escape(ev.pin or EMPTY_PIN_LABEL)}")
        elif isinstance(ev, WpsPskRecovered):
            msg = (f'[bold green]✓ WPS PSK[/bold green] on '
                   f'[bold cyan]{ap_label}[/bold cyan] = "{escape(ev.psk)}"')
        elif isinstance(ev, WpsPbcPskRecovered):
            msg = (f'[bold green]✓ WPS PSK[/bold green] [dim](via PushButton)[/dim] on '
                   f'[bold cyan]{ap_label}[/bold cyan] = "{escape(ev.psk)}"')
        else:
            return  # eapol events suppressed in scanner
        # Leading space aligns the ✓ win with the ● / ├─► / └─► tree log above it.
        self._write_log(Text.from_markup(f" {msg}", emoji=False))
        if isinstance(ev, (HandshakeCaptured, PmkidCaptured)):
            save_result = ev.save_result
            if save_result is None:
                reason = "not saved: SSID unknown" if not ev.ssid else "not saved"
                self._write_log(Text.from_markup(treelog.leaf(f"[dim]({reason})[/dim]"), emoji=False))
            else:
                verb = "saved" if save_result.was_new else "already saved as"
                self._write_log(Text.from_markup(treelog.leaf(
                    f"[dim]({verb} {escape(save_result.path.name)})[/dim]"
                    f"{saved_psk_markup(save_result.matches_saved_psk)}"), emoji=False))

    def _write_log(self, text) -> None:
        try:
            log = self.query_one("#system-log", RichLog)
        except NoMatches:
            return
        # Bypass RichLog's emojis (would turn :ab: / :cd: inside a BSSID into 🆎 / 💿).
        if isinstance(text, str):
            text = Text.from_markup(text, emoji=False)
        log.write(text)

    # ----- Sort --------------------------------------------------------------

    def _should_sort(self) -> bool:
        delay = Config.scanner_sort_delay
        if delay < 0:
            return False
        return (time.time() - self._last_sort_time) >= delay

    # ----- Actions -----------------------------------------------------------

    def action_toggle_log(self) -> None:
        log_widget = self.query_one("#system-log")
        log_widget.display = not log_widget.display

    def selected_ap(self) -> Optional[AccessPoint]:
        bssid = self.query_one("#ap-table", APTable).cursor_bssid
        return self.ap_cache.get(bssid) if bssid else None

    # ----- WPS PBC opportunistic capture -------------------------------------

    def action_wps_pbc_mode(self) -> None:
        """Toggle WPS PBC auto-invade on/off (ON by default)."""
        self.app.pbc_enabled = not self.app.pbc_enabled
        Config.wps_auto_pbc = self.app.pbc_enabled
        self.app.persist_config()
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
            if self.app.vault.has_psk(ap):
                ssid = escape(ap.ssid or ap.bssid)
                self._write_log(f"  [dim]({ssid} already captured, PSK: [bold]{escape(self.app.vault.known_psk(ap) or '?')}[/bold])[/dim]")
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

    def _log_auto_crack_status_when_changed(self) -> None:
        enabled = Config.auto_crack is not None
        if enabled == self._logged_auto_crack:
            return
        self._logged_auto_crack = enabled
        if enabled:
            self._write_log(treelog.header(
                "[bold]Auto-crack[/bold] is [bold green]enabled[/bold green] "
                f"[dim]({Config.auto_crack}, change in Prefs: [bold]ctrl+p[/bold])[/dim]", color="green"))
        else:
            self._write_log(treelog.header(
                "[bold]Auto-crack[/bold] is [orange1]disabled[/orange1] "
                "[dim](change in Prefs: [bold]ctrl+p[/bold])[/dim]", color="orange1"))

    def on_wifite_app_job_cracked(self, message: "WifiteApp.JobCracked") -> None:
        job = message.job
        status = self.app.vault.manager.key_status(job) or PasswordStatus.CURRENT
        if status is PasswordStatus.CURRENT:
            label = "[bold green]✓ WPA PSK[/bold green]"
        else:
            label = f"[bold yellow]✓ WPA PSK ({status.value})[/bold yellow]"
        self._write_log(Text.from_markup(
            f' {label} [dim](via {escape(job.display_name)})[/dim] '
            f'= "{escape(job.key or "")}"', emoji=False))

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
        if self.app.vault.has_psk(ap):
            wps = self.app.vault.wps_capture(ap)
            where = f" [dim]({escape(Path(wps.path).name)})[/dim]" if wps else ""
            self._write_log(treelog.leaf(f"[italic]already captured[/italic]{where}"))
            return
        if self._pbc_capturing:
            return
        self.run_worker(self._invade_pbc(ap))

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
                    result = self.app.vault.save_wps_pbc(ap, outcome.psk)
                except OSError:
                    result = None
                if result is None:
                    self._write_log(treelog.leaf("[dim](PSK not saved to disk)[/dim]"))
                else:
                    verb = "saved" if result.was_new else "already saved as"
                    self._write_log(treelog.leaf(
                        f"[cyan]{verb}[/cyan] [dim]{escape(result.path.name)}[/dim]"))
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

    def action_open_vault(self) -> None:
        self.app.action_toggle_vault()

    def action_focus_filter(self) -> None:
        self.query_one(FilterBar).focus_text()

    def action_focus_encryption(self) -> None:
        self.query_one(FilterBar).focus_encryption()

    def action_cycle_sort(self) -> None:
        table = self.query_one("#ap-table", APTable)
        index = COLUMN_KEYS.index(table.sort_column)
        table.sort_column = COLUMN_KEYS[(index + 1) % len(COLUMN_KEYS)]
        self._persist_sort()

    def action_toggle_sort_dir(self) -> None:
        table = self.query_one("#ap-table", APTable)
        table.sort_reverse = not table.sort_reverse
        self._persist_sort()

    def _persist_sort(self) -> None:
        table = self.query_one("#ap-table", APTable)
        Config.scanner_sort = table.sort_column
        Config.scanner_sort_reverse = table.sort_reverse
        self.app.persist_config()

    def action_scroll_home(self) -> None:
        self.query_one("#ap-table", APTable).move_cursor(0, animate=True)

    def action_scroll_end(self) -> None:
        table = self.query_one("#ap-table", APTable)
        table.move_cursor(table.row_count - 1, animate=True)

    def action_change_channel(self) -> None:
        array = self.app.array
        if not array:
            self._write_log("[bold red][!] No active interface.[/bold red]")
            return

        supported = array.supported_channels
        if not supported:
            self._write_log("[bold red][!] Driver did not declare SUPPORTED_CHANNELS.[/bold red]")
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
            self._write_log("[dim]Channel filter unchanged.[/dim]")
        else:
            await self._apply_channel_filter(result)
        self.query_one(FilterBar).set_channels(self._channel_filter)
        self.query_one("#ap-table", APTable).focus()

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

        pieces = [
            f"[bold cyan]{name}[/bold cyan] [dim]({rngs})[/dim]"
            for name, rngs in band_ranges(channels)
        ]
        summary = " and ".join(pieces) if pieces else "[dim]no channels[/dim]"
        self._write_log(f" [dim]●[/dim] [bold]Channel hopping[/bold] across {summary}")
        if dropped:
            noun = "AP" if dropped == 1 else "APs"
            self._write_log(treelog.leaf(f"[dim]Cleared [bold]{dropped}[/bold] "
                                         f"{noun} outside the filter[/dim]"))

    # ----- Filter bar --------------------------------------------------------

    def on_filter_bar_scan_filter_changed(self, message: FilterBar.ScanFilterChanged) -> None:
        if message.scan_filter.hide_silenced != Config.hide_silenced:
            Config.hide_silenced = message.scan_filter.hide_silenced
            self.app.persist_config()   # the text query re-emits per keystroke; only the box writes
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

    async def on_ap_table_row_selected(self, event: APTable.RowSelected) -> None:
        target_ap = self.ap_cache.get(event.bssid)
        if target_ap:
            self.app.target_ap = target_ap
            self.app.push_screen("focus")

    def on_ap_table_sort_changed(self, event: APTable.SortChanged) -> None:
        self._persist_sort()
