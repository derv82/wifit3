import asyncio
import logging
import re
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, DataTable, Button, Label, RichLog
from textual.containers import Vertical, Horizontal
from textual.binding import Binding
from rich.text import Text
from rich.markup import escape

from wifit3.engine.models import AccessPoint
from wifit3.engine.attacks import treelog
from wifit3.engine.hc22000 import write_hc22000
from wifit3.engine.pcap import write_pcap
from wifit3.engine.attacks.pmkid_harvest import PmkidHarvestAttack
from wifit3.engine.attacks.sae_probe import SAEGroupProbeAttack
from wifit3.engine.attacks.wpa3_downgrade import WPA3DowngradeAttack
from wifit3.engine.attacks.wep.campaign import WepCampaign
from wifit3.engine.attacks.wep.crack import CRACK_READY_THRESHOLD
from wifit3.engine.attacks.wps.campaign import WpsCampaign
from wifit3.engine.attacks.wps.pbc import WpsPbcCapture, save_pbc_credential
from wifit3.engine.attacks.wps.registrar import PinResult

from ..capture_events import DECLOAK_METHOD_LABELS, CaptureEvent, CaptureEventDetector
from ..encryption_format import (
    format_encryption_markup,
    format_pmf_markup,
)

logger = logging.getLogger(__name__)


def _wep_key_chip(key_hex: Optional[str]) -> str:
    """Black-bold-on-cyan WEP key chip: `<hex> = "ascii"` when the key is
    printable ASCII (e.g. `abcde`), bare hex otherwise (e.g. a 104-bit key)."""
    if not key_hex:
        return "[dim]?[/dim]"
    try:
        kb = bytes.fromhex(key_hex)
    except ValueError:
        inner = key_hex
    else:
        if kb and all(0x20 <= b < 0x7F for b in kb):
            inner = f'{key_hex} = "{kb.decode("ascii")}"'
        else:
            inner = key_hex
    return f"[black bold on cyan] {inner} [/black bold on cyan]"


def _format_duration(seconds: int) -> str:
    """Human-readable duration for the Focus 'Last Beacon' line.
    Examples: '5s', '1m 12s', '1h 4m', '2d 3h'. Drops the lower unit
    when it's zero to keep the line tight."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s" if s else f"{m}m"
    if seconds < 86400:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h}h {m}m" if m else f"{h}h"
    d, rem = divmod(seconds, 86400)
    h = rem // 3600
    return f"{d}d {h}h" if h else f"{d}d"


class FocusView(Screen):
    """The Attack/Focus mode for a specific AP."""

    BINDINGS = [
        Binding("escape", "go_back", "Back to Scanner", show=True),
        Binding("q", "app.quit", "Quit", show=True),
        # 's' has two labels for the same key — check_action() reveals exactly
        # one based on the target's encryption (Save Capture for WPA's
        # handshake/PMKID, Save Key for a cracked WEP key) and hides both until
        # there's actually something to save. Likewise 'c' only appears once a
        # WEP key is recovered.
        Binding("s", "save_capture", "Save Capture", show=True),
        Binding("s", "save_key", "Save Key", show=True),
        Binding("c", "copy_key", "Copy WEP Key", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.target_ap: AccessPoint = None
        self._refresh_timer = None
        self._known_clients: Set[str] = set()
        # Sliding-window samples of (timestamp, ap.beacons) for a recent
        # beacons/s rate instead of a since-first-seen average (see update_ui).
        self._beacon_samples: deque = deque()
        # Granular: also surfaces every new EAPOL frame, not just completions.
        self._events = CaptureEventDetector(granular_eapol=True)
        # WPA3 Downgrade is a long-running probe-response-spoof daemon. Held
        # here so the button can toggle Start/Stop and so target/screen
        # transitions can tear it down deterministically.
        self._wpa3_down_attack: Optional[WPA3DowngradeAttack] = None
        # WEP "Generate IVs" campaign (M3): fake-auth + ARP replay. Held so the
        # button toggles Start/Stop and transitions tear it down deterministically.
        self._wep_campaign: Optional[WepCampaign] = None
        # WPS PIN brute-force campaign (Reaver/Bully-style two-halves sweep).
        # Held so the button toggles Start/Stop and target/screen transitions
        # tear it down deterministically.
        self._wps_campaign: Optional[WpsCampaign] = None
        # WPS PBC auto-capture: while focused on a target, a detected push-button
        # window is grabbed automatically (we're already on-channel and this is
        # clearly a target of interest). One task at a time; once-per-BSSID.
        self._pbc_task: Optional[asyncio.Task] = None
        self._pbc_done: Set[str] = set()

    # ----- Compose / mount ---------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        with Vertical(id="focus-container"):
            # Two columns. LEFT (fixed width): TARGET heads a stack of actions
            # (ATTACKS) + the client list + CLIENT DEAUTH — all share one width,
            # so TARGET lines up with CLIENTS and the attack buttons get room.
            # RIGHT: the SECURITY | CAPTURE summary row on top (so the visual
            # top row reads TARGET | SECURITY | CAPTURE), then the tall EVENT LOG.
            with Horizontal(id="main-row"):
                with Vertical(id="left-col"):
                    yield Vertical(
                        Label("TARGET INFO", classes="panel-title"),
                        Label(id="lbl-ssid"),   # centered chip (see #lbl-ssid)
                        # Detail lines as a left-aligned block, centered as a
                        # group under the chip.
                        Vertical(
                            Label(id="lbl-bssid"),
                            Label(id="lbl-channel"),
                            Label(id="lbl-last-beacon"),
                            classes="panel-body",
                        ),
                        classes="info-box", id="panel-target",
                    )
                    # ATTACKS — NO title bar: the buttons self-label, and the
                    # family is stated by TARGET's "Encryption:" line. Crypto set
                    # toggles via update_ui (ids unchanged). 2-per-row:
                    #   WEP: Replay Chop / Save Frag   WPA: PMKID SAE / WPA↓ Save
                    with Vertical(classes="info-box", id="attack-panel"):
                        with Horizontal(classes="button-row"):
                            yield Button("Replay", variant="success", id="btn-gen-ivs")
                            yield Button("Chop", variant="primary", id="btn-chop")
                            yield Button("PMKID", variant="primary", id="btn-pmkid")
                            yield Button("WPS PIN", variant="primary", id="btn-wps-pin", disabled=True)
                        with Horizontal(classes="button-row"):
                            yield Button("WPA ↓", variant="primary", id="btn-wpa3-down", disabled=True)
                            yield Button("Save", variant="success", id="btn-save", disabled=True)
                            yield Button("Frag", variant="primary", id="btn-frag")
                    with Vertical(classes="info-box", id="client-panel"):
                        yield Label("CLIENTS", classes="panel-title", id="lbl-clients-title")
                        client_table = DataTable(cursor_type="row", id="client-table")
                        client_table.add_column("MAC Address", key="mac")
                        client_table.add_column("POWER", key="signal")
                        client_table.add_column("PKTS", key="packets")
                        yield client_table
                        # Client-targeted deauth lives directly under the CLIENTS
                        # list (no separate titled panel). Flat, 2-line labels,
                        # edge-to-edge so the pair stays aligned.
                        with Horizontal(classes="button-row", id="deauth-row"):
                            yield Button("Deauth\nSelected", variant="warning", id="btn-deauth-sel", disabled=True)
                            yield Button("Deauth\nBroadcast", variant="error", id="btn-deauth-bcast")

                with Vertical(id="right-col"):
                    # Top-right summary row: SECURITY | CAPTURE (aligns with
                    # TARGET to form the "TARGET | SECURITY | CAPTURE" header).
                    with Horizontal(id="top-right"):
                        yield Vertical(
                            Label("SECURITY", classes="panel-title"),
                            Label(id="lbl-enc"),
                            Label(id="lbl-wps"),
                            Label(id="lbl-wps-status"),     # live WPS PIN campaign progress
                            Label(id="lbl-pmf"),
                            Label(id="lbl-wpa3"),
                            Label(id="lbl-sae-groups"),
                            # WPA3-Down live status (hidden unless the daemon is
                            # running) — live probe counts live here, not as one
                            # event-log line per probe.
                            Label(id="lbl-wpa3-down"),
                            # WEP-only: fake-auth status + Crack progress (runs
                            # in PARALLEL with capture, hence under SECURITY).
                            Label(id="lbl-fakeauth"),
                            Label(id="lbl-crack"),
                            Label(id="lbl-crack-info"),
                            classes="info-box", id="panel-security",
                        )
                        yield Vertical(
                            Label("CAPTURE", classes="panel-title"),
                            Label(id="lbl-beacons"),
                            Label(id="lbl-pwr"),
                            Label(id="lbl-handshake"),
                            Label(id="lbl-pmkid"),
                            # WEP-only (in place of Handshake/PMKID): IV count +
                            # a dedicated Replay-status row.
                            Label(id="lbl-ivs"),
                            Label(id="lbl-replay"),
                            classes="info-box", id="panel-capture",
                        )
                    # The stretchy panel — fills the rest of the right column.
                    with Vertical(classes="info-box", id="event-log-panel"):
                        yield Label("EVENT LOG", classes="panel-title")
                        yield RichLog(id="focus-event-log", markup=True, highlight=False, wrap=True)

        yield Footer()

    async def on_mount(self) -> None:
        self._refresh_timer = self.set_interval(1 / 10, self.update_ui)
        await self._init_target()

    async def on_screen_resume(self) -> None:
        await self._init_target()

    async def _init_target(self) -> None:
        # Always tear down any running WPA3 Down daemon — its forged template
        # is bound to the PREVIOUS target's BSSID/SSID/channel, so it'd inject
        # the wrong payload for a new target. Safe no-op if nothing's running.
        self._stop_wpa3_down()
        # Same for the Generate IVs campaign — its forged STA + replay are
        # bound to the previous BSSID; leaving it running would inject to the
        # wrong AP.
        self._stop_generate_ivs()
        self._stop_pbc_capture()
        self._stop_wps_pin()

        self.target_ap = getattr(self.app, "target_ap", None)
        if not self.target_ap:
            return

        # Reset per-target state.
        self._known_clients.clear()
        self._beacon_samples.clear()
        self._events.reset()
        self.query_one("#client-table", DataTable).clear()
        self.query_one("#focus-event-log", RichLog).clear()

        ssid_chip = (
            f"[black bold on cyan] {escape(self.target_ap.ssid)} [/black bold on cyan]"
            if self.target_ap.ssid
            else "[dim italic]<hidden>[/dim italic]"
        )
        self._log(f"[bold]Target acquired:[/bold] {ssid_chip}")

        # BSSID is the └─► terminal when there's no interface to tune; otherwise
        # it's a ├─► branch and the tune line closes the group. Keeps the tree
        # leaf-terminated on both paths (RichLog is append-only — no rewrites).
        iface = getattr(self.app, "active_interface", None)
        bssid_line = f"[dim]BSSID:[/dim] [white]{self.target_ap.bssid}[/white]"
        if iface:
            self._log(treelog.branch(bssid_line))
            ok = await iface.set_channel(self.target_ap.channel)
            if ok:
                self._log(treelog.leaf(f"Tuned to [cyan]channel {self.target_ap.channel}[/cyan]"))
            else:
                self._log(treelog.leaf(
                    f"[yellow]Tried to tune to channel {self.target_ap.channel}[/yellow]"
                ))
        else:
            self._log(treelog.leaf(bssid_line))

        # One-time warning: PMF Required → deauth-based attacks (handshake
        # capture via client deauth, PBC race via deauth, broadcast deauth)
        # will silently fail because the AP rejects unauthenticated mgmt frames.
        if self.target_ap.pmf_required:
            self._log("[yellow][!] PMF Required[/yellow] — deauth-based attacks "
                      "won't work on this AP (handshake/PBC race via deauth, "
                      "broadcast deauth). PMKID / WPS / passive capture still fine.")

        self._log_persisted_history(self.target_ap)
        self.update_ui()

    def _log_persisted_history(self, ap: AccessPoint) -> None:
        """On target acquisition, surface saved captures/ artifacts for this AP
        so the user knows exactly what's already on disk (and how stale) — the
        same data that lights the Scanner badges."""
        if not ap.persisted:
            return
        self._log("[bold]Previous captures found in[/bold] [cyan]./captures/*[/cyan]:")
        last = len(ap.persisted) - 1
        for i, cap in enumerate(ap.persisted):
            line = treelog.leaf if i == last else treelog.branch
            dt = datetime.fromtimestamp(cap.timestamp)
            ts = f"[dim]{dt:%Y-%m-%d %H:%M}[/dim]"
            if cap.kind == "WEP":
                self._log(line(f"[bold cyan]{'WEP Key:':<9}[/bold cyan] "
                               f"{_wep_key_chip(cap.value)}  {ts}"))
            elif cap.kind == "WPS":
                self._log(line(f"[bold cyan]{'WPS PSK:':<9}[/bold cyan] "
                               f"[black bold on cyan] {escape(cap.value or '?')} "
                               f"[/black bold on cyan]  {ts}"))
            else:
                label = "Handshake" if cap.kind == "HS" else "PMKID"
                self._log(line(
                    f"[bold cyan]{label:<9}[/bold cyan] "
                    f"[white]{dt:%Y-%m-%d}[/white] [dim]{dt:%H:%M}[/dim]"))

    # ----- Per-tick UI refresh -----------------------------------------------

    def update_ui(self) -> None:
        if not self.target_ap or not self.is_current:
            return

        ap = self.target_ap
        is_wep = (ap.encryption or "").upper() == "WEP"

        # Opportunistic WPS PBC: if a push-button window opens on this target,
        # auto-capture the PSK. We're already on-channel; gated to one attempt at
        # a time, once per BSSID, and only when no other TX activity owns the radio.
        if (ap.wps_pbc_active and not self._pbc_busy()
                and ap.bssid not in self._pbc_done
                and self._wep_campaign is None and self._wpa3_down_attack is None
                and self._wps_campaign is None):
            self._pbc_task = asyncio.create_task(self._auto_capture_pbc(ap))

        # TARGET INFO panel. SSID as a chip (no "ESSID:" prefix) so short names
        # like "NETGEAR" are still visible; truncated with … to fit the panel.
        if ap.ssid:
            maxname = 24   # TARGET inner (26) minus the chip's 2 padding spaces
            shown = (ap.ssid if len(ap.ssid) <= maxname
                     else ap.ssid[:maxname - 1].rstrip() + "…")
            ssid_markup = f"[black on cyan] {escape(shown)} [/black on cyan]"
        else:
            ssid_markup = "[italic cyan]‹hidden›[/italic cyan]"
        self.query_one("#lbl-ssid", Label).update(
            Text.from_markup(ssid_markup, emoji=False)
        )
        self.query_one("#lbl-bssid", Label).update(Text(f"BSSID: {ap.bssid}"))
        # While in FocusView the hopper is always stopped — channel is locked.
        self.query_one("#lbl-channel", Label).update(
            Text.from_markup(
                f"Channel: {ap.channel} [green](Locked)[/green]",
                emoji=False,
            )
        )
        # Last Beacon: most active targets sit at 0s, so colour by recency —
        # green "now", orange for a brief gap, red once it's been quiet >10s.
        last_seen_s = max(0, int(time.time() - ap.last_seen))
        if last_seen_s == 0:
            beacon = "[green]now[/green]"
        elif last_seen_s <= 10:
            beacon = f"[orange1]{last_seen_s}s[/orange1]"
        else:
            beacon = f"[red]{_format_duration(last_seen_s)}[/red]"
        self.query_one("#lbl-last-beacon", Label).update(
            Text.from_markup(f"Last Beacon: {beacon}", emoji=False)
        )

        # SECURITY panel.
        self.query_one("#lbl-enc", Label).update(
            Text.from_markup(
                "Encryption: " + format_encryption_markup(ap, detailed=True),
                emoji=False,
            )
        )
        # WPS + PMF share one line (both short, static per target). WPS only
        # shows if present; PMF only in RSN (WPA2/3 — for WEP/OPEN there's no
        # RSN and it's always Disabled, so we omit it). The standalone lbl-pmf
        # slot is folded in here.
        # Compact WPS: version + a lock glyph (green 🔓 unlocked = attackable,
        # red 🔒 locked = dead end). The verbose method list doesn't fit here.
        # The static WPS/PMF row stays put (so the 🔒/🔓 lock icon is always
        # visible). A second dedicated row (lbl-wps-status) carries the live PIN
        # campaign status when it's running — hidden otherwise.
        wps_label = self.query_one("#lbl-wps", Label)
        if ap.wps:
            lock = "[red]🔒[/red]" if ap.wps_locked else "[green]🔓[/green]"
            ver = f"{ap.wps_version} " if ap.wps_version else ""
            wps_part = f"WPS: {ver}{lock}"
        else:
            wps_part = None
        pmf_part = f"PMF: {format_pmf_markup(ap)}" if (ap.akms or ap.wpa3) else None
        parts = [p for p in (wps_part, pmf_part) if p]
        if parts:
            wps_label.display = True
            wps_label.update(Text.from_markup("  ·  ".join(parts), emoji=False))
        else:
            wps_label.display = False

        # Auto-close a finished campaign so its tree closes with the success
        # leaves (cyan PIN + green PSK), the button reverts to "WPS PIN", and the
        # status row hides. The recovered PSK lives on in captures/ + persisted
        # history; the in-memory campaign object isn't needed past success.
        if self._wps_campaign is not None and self._wps_campaign.state.phase == "done":
            self._stop_wps_pin()

        wps_status_label = self.query_one("#lbl-wps-status", Label)
        if self._wps_campaign is not None:
            wps_status_label.display = True
            wps_status_label.update(Text.from_markup(
                self._wps_status_markup(self._wps_campaign), emoji=False))
        else:
            wps_status_label.display = False
        self.query_one("#lbl-pmf", Label).display = False

        # WPA3-mode line dropped — redundant with the Encryption line, which
        # already states e.g. "WPA3→2 (transition)".
        self.query_one("#lbl-wpa3", Label).display = False

        # SAE Groups — populated by the SAE probe attack, cached on the AP.
        # Hidden until we have at least one probe result. Shows only the
        # Dragonblood groups (22/23/24): green supported, dim rejected.
        sae_label = self.query_one("#lbl-sae-groups", Label)
        if ap.sae_groups:
            sae_label.display = True
            sae_label.update(
                Text.from_markup(
                    f"SAE Groups: {self._format_sae_groups_markup(ap)}",
                    emoji=False,
                )
            )
        else:
            sae_label.display = False

        # WPA3-Down live status — shown only while the daemon is running. This
        # is where the per-probe counts surface (instead of flooding the log).
        down_label = self.query_one("#lbl-wpa3-down", Label)
        if self._wpa3_down_attack:
            st = self._wpa3_down_attack.stats
            down_label.display = True
            down_label.update(Text.from_markup(
                f"WPA2↓: [bold green]✓ ON[/bold green] "
                f"[dim]({st.directed_probes} dir., {st.wildcard_probes} wild.)[/dim]",
                emoji=False,
            ))
        else:
            down_label.display = False

        # Attack buttons. WEP and WPA targets get disjoint button sets — the
        # WPA buttons (PMKID/SAE/WPA3 Down) are meaningless for WEP, so hide
        # (not just disable) them and surface Fake Auth in their place.
        btn_wps = self.query_one("#btn-wps-pin", Button)
        btn_down = self.query_one("#btn-wpa3-down", Button)
        btn_pmkid = self.query_one("#btn-pmkid", Button)
        btn_gen = self.query_one("#btn-gen-ivs", Button)
        btn_frag = self.query_one("#btn-frag", Button)
        btn_chop = self.query_one("#btn-chop", Button)

        # (ATTACKS panel has no title now — the buttons + TARGET's Encryption
        # line convey the family.) Each button is shown only when the attack can
        # plausibly work against this AP — "useless disabled buttons" just clutter
        # the panel (the security row + tooltip-via-log convey what's missing).
        # WPS PIN: WPA-only with WPS (WEP+WPS is a narrow historical slice).
        btn_wps.display = not is_wep and bool(ap.wps)
        # WPA Downgrade only works against WPA3-transition APs.
        btn_down.display = bool(ap.wpa3 and ap.transition_mode)
        # PMKID is dead on pure SAE (uncrackable). Show for WPA/WPA2 and for
        # WPA3-transition (the WPA2 portion is the attack surface).
        btn_pmkid.display = not is_wep and ((not ap.wpa3) or ap.transition_mode)
        # Replay vanishes once the key is cracked — Save takes its place, so it
        # reads as a "Replay → Save" swap.
        btn_gen.display = is_wep and ap.wep_key is None
        # Frag/Chop are visible for any WEP target (before crack), but disabled
        # until Replay starts the campaign — they're sub-modes of it, and the
        # campaign owns the fake-auth they need. Visible-but-grey reads cleaner
        # than vanish-on-Replay-click ("where did they come from?").
        btn_frag.display = is_wep and ap.wep_key is None
        btn_chop.display = is_wep and ap.wep_key is None

        if is_wep:
            # A finished campaign (key recovered) is torn down so the button
            # reverts to "Generate IVs" — the user shouldn't have to click Stop
            # after a successful crack. The key persists on ap.wep_key.
            camp = self._wep_campaign
            if camp is not None and camp.recovered_key is not None:
                self._stop_generate_ivs()
                camp = None
            # Replay is the campaign switch: green to start, red to STOP the
            # whole campaign. Frag/Chop are sub-attacks: blue to start, orange
            # ("Stop X") to stop just that one (the campaign keeps running).
            running = camp is not None
            btn_gen.label = "Stop Replay" if running else "Replay"
            btn_gen.variant = "error" if running else "success"
            # Frag/Chop visibility set above (any WEP target before crack); here
            # we just gate the disabled state on whether the campaign is running.
            fragging = bool(camp and camp.frag_active)
            btn_frag.label = "Stop Frag" if fragging else "Frag"
            btn_frag.variant = "warning" if fragging else "primary"
            btn_frag.disabled = not running
            chopping = bool(camp and camp.chop_active)
            btn_chop.label = "Stop Chop" if chopping else "Chop"
            btn_chop.variant = "warning" if chopping else "primary"
            btn_chop.disabled = not running
            self._update_fakeauth_line()
        else:
            self.query_one("#lbl-fakeauth", Label).display = False
            self.query_one("#lbl-crack", Label).display = False
            self.query_one("#lbl-crack-info", Label).display = False
            # Per-attack eligibility, gated by the cross-attack TX mutex.
            # WPA3 Down: only against transition-mode APs (pure WPA3 clients
            # refuse a WPA2-only ad from a known-SAE network). When running,
            # always enabled so Stop works.
            wpa3down_eligible = bool(ap.wpa3 and ap.transition_mode)
            if self._wpa3_down_attack is not None:
                btn_down.disabled = False
            else:
                btn_down.disabled = (not wpa3down_eligible
                                     or self._other_long_running_tx(exclude="wpa3down"))
            btn_down.label = "Stop ↓" if self._wpa3_down_attack else "WPA ↓"

            # PMKID: pure SAE PMKID isn't crackable, so only WPA2 + WPA3-Transition.
            # One-shot, so no self-exclusion — any other long-running TX blocks it.
            pmkid_eligible = (not ap.wpa3) or ap.transition_mode
            btn_pmkid.disabled = not pmkid_eligible or self._other_long_running_tx()

            # WPS PIN: Start/Stop toggle, AP must be WPS-capable + unlocked.
            if self._wps_campaign is not None:
                btn_wps.label = "Stop PIN"
                btn_wps.variant = "error"
                btn_wps.disabled = False
            else:
                btn_wps.label = "WPS PIN"
                btn_wps.variant = "primary"
                wps_eligible = bool(ap.wps and not ap.wps_locked)
                btn_wps.disabled = (not wps_eligible
                                    or self._other_long_running_tx(exclude="wps"))

        # CAPTURE panel (dynamic). Beacon RATE is windowed over the last few
        # seconds, not averaged since first_seen — an average converges to a
        # flat line and hides how RX is doing *right now* (e.g. you moved, or
        # the AP got busy). Total count stays cumulative.
        now = time.time()
        self._beacon_samples.append((now, ap.beacons))
        BEACON_WINDOW_S = 5.0
        while len(self._beacon_samples) > 1 and now - self._beacon_samples[0][0] > BEACON_WINDOW_S:
            self._beacon_samples.popleft()
        oldest_t, oldest_n = self._beacon_samples[0]
        span = now - oldest_t
        # Need ~a second of window before the rate means anything.
        rate_str = f"{(ap.beacons - oldest_n) / span:.1f}/s" if span >= 1.0 else "…/s"
        self.query_one("#lbl-beacons", Label).update(
            f"Beacons: {ap.beacons:,} ({rate_str})"
        )
        self.query_one("#lbl-pwr", Label).update(f"Power: {ap.signal} dBm")

        # WEP and WPA2/3 capture progress are mutually exclusive: WEP has no
        # handshake/PMKID, WPA has no IVs. Show whichever pair fits the target.
        hs_label = self.query_one("#lbl-handshake", Label)
        pmkid_label = self.query_one("#lbl-pmkid", Label)
        ivs_label = self.query_one("#lbl-ivs", Label)

        hs_label.display = not is_wep
        pmkid_label.display = not is_wep
        ivs_label.display = is_wep
        self.query_one("#lbl-replay", Label).display = is_wep

        if is_wep:
            # _update_wep_capture owns lbl-ivs / lbl-replay (CAPTURE) and
            # lbl-crack / lbl-crack-info (SECURITY).
            self._update_wep_capture(ap)
        else:
            # Count distinct captured handshake INSTANCES (by ANonce), so a
            # client that re-handshakes several times shows x2, x3, … instead of
            # collapsing to x1 (one Handshake object per client). Matches the
            # per-instance "Valid 4-Way Handshake" log.
            n_complete = sum(
                hs.complete_instances for hs in ap.handshakes.values()
            )
            n_partial = sum(
                1
                for hs in ap.handshakes.values()
                if not hs.is_complete and hs.total_eapol_frames > 0
            )
            # Per-message tally across this AP's handshakes. The 4-way validity
            # logic dedups by (msg, replay), so repeated M1/M3 retries collapse
            # to one — but the user still wants to see those frames landing as
            # progress, so surface the raw-frame counts here (matches the log).
            msg_counts: Counter = Counter()
            for hs in ap.handshakes.values():
                for f in hs.eapol_frames:
                    if f.msg_num:
                        msg_counts[f.msg_num] += 1
            breakdown = " · ".join(f"M{m}×{msg_counts[m]}" for m in sorted(msg_counts))
            # Persisted (captures/) counts back-fill the live ones: if we have
            # nothing live but a saved capture exists, show it green + (history)
            # so the badge on Scanner has a matching explanation here.
            persisted_hs = sum(1 for p in ap.persisted if p.kind == "HS")
            persisted_pmkid = sum(1 for p in ap.persisted if p.kind == "PMKID")
            if n_complete:
                hs_text = f"[bold green]Captured x{n_complete}[/bold green]"
                if n_partial:
                    hs_text += f" [dim](+{n_partial} partial)[/dim]"
            elif n_partial:
                hs_text = f"[yellow]Partial[/yellow] [dim]{breakdown}[/dim]"
            elif persisted_hs:
                hs_text = (f"[bold green]Captured x{persisted_hs}[/bold green] "
                           f"[dim](history)[/dim]")
            else:
                hs_text = "[dim]Not captured[/dim]"
            hs_label.update(Text.from_markup(f"Handshake: {hs_text}", emoji=False))

            n_pmkid = sum(1 for hs in ap.handshakes.values() if hs.pmkid)
            if n_pmkid:
                pmkid_text = f"[bold green]Captured x{n_pmkid}[/bold green]"
            elif persisted_pmkid:
                pmkid_text = (f"[bold green]Captured x{persisted_pmkid}[/bold green] "
                              f"[dim](history)[/dim]")
            else:
                pmkid_text = "[dim]Not captured[/dim]"
            pmkid_label.update(
                Text.from_markup(f"PMKID:     {pmkid_text}", emoji=False)
            )

        # Save button.
        # Save only appears once there's something to save (a WEP key, or a
        # WPA handshake/PMKID) — hidden, not shown-disabled. It's created
        # disabled (compose), so clear that when shown or it greys out.
        btn_save = self.query_one("#btn-save", Button)
        # Keep Save's grid cell reserved (visibility, not display) so its
        # row-mate Frag stays pinned in the right column even before there's
        # anything to save — otherwise a display:none Save collapses the row and
        # Frag slides under Replay.
        btn_save.display = True
        btn_save.visible = ap.has_capture
        btn_save.disabled = not ap.has_capture
        # Button stays plain "Save" to fit the narrow panel; the footer 's'
        # carries the crypto-specific label (Save Capture / Save Key).
        # Re-evaluate the footer's Save/Copy keys (check_action) as the key /
        # handshake state changes.
        self.refresh_bindings()

        # Clients.
        iface = getattr(self.app, "active_interface", None)
        if iface:
            self._refresh_clients(iface)

        # Drain new capture events into the log.
        self._drain_capture_events(ap, iface.forged_macs if iface else set())

        # CLIENTS (N) title + DEAUTH 'Selected' enabled only with a highlighted row.
        n_clients = self.query_one("#client-table", DataTable).row_count
        self.query_one("#lbl-clients-title", Label).update(f"CLIENTS ({n_clients})")
        # Deauth bursts (one-shot) — gated by the cursor, the cross-attack TX
        # mutex, AND PMF: a PMF-Required AP rejects unauthenticated deauth, so
        # the attack does nothing. The "PMF Required → attacks won't work"
        # warning is also logged once on target acquisition (_init_target).
        other_tx = self._other_long_running_tx()
        deauth_blocked = other_tx or ap.pmf_required
        self.query_one("#btn-deauth-sel", Button).disabled = (
            self._cursor_mac() is None or deauth_blocked)
        self.query_one("#btn-deauth-bcast", Button).disabled = deauth_blocked

    @staticmethod
    def _replay_status_markup(campaign) -> str:
        """The Replay-status row's value. Surfaces frag/chop too — while they
        run, replay is paused on purpose, so say WHAT'S running (not a bare
        'paused' that reads like the attack stalled)."""
        if campaign is None:
            return "[dim]not started[/dim]"
        # Frag/Chop take over the radio (replay paused by design) — name them.
        if campaign.frag_active:
            return "[dim]forging a seed using[/dim] [cyan]Fragmentation[/cyan][dim]…[/dim]"
        if campaign.chop_active:
            return "[dim]forging a seed using[/dim] [cyan]ChopChop[/cyan][dim]…[/dim]"
        s = campaign.replay.state
        if s == "replaying":
            # target_pps = the smooth P&O rate, not the jittery per-cycle
            # measured effective_pps.
            return (
                f"[green]Replaying ARP[/green] "
                f"[dim]({campaign.replay.target_pps:.0f}pps)[/dim]"
            )
        if s == "testing":
            return "[cyan]Trying candidate ARP…[/cyan]"
        if s == "waiting-arp":
            return "[yellow]waiting for ARP[/yellow]"
        if s == "waiting-auth":
            return "[dim]associating…[/dim]"
        if s == "paused":
            return "[dim]paused[/dim]"
        return "[dim]idle[/dim]"

    def _update_wep_capture(self, ap: AccessPoint) -> None:
        """WEP CAPTURE rows — IVs + a dedicated Replay-status row — plus the
        Crack section (under SECURITY). The usable-IV (crack-sample) count is no
        longer shown here; it lives in SECURITY's Crack line (N/10k usable IVs),
        which is what gates cracking."""
        iface = getattr(self.app, "active_interface", None)
        n = ap.wep.unique_ivs if ap.wep else 0
        rate = iface.wep_store.rate(ap.bssid) if iface else 0.0
        samples = iface.wep_store.crack_sample_count(ap.bssid) if iface else 0
        campaign = self._wep_campaign

        count = f"[bold green]{n:,}[/bold green]" if n else "[red]0[/red]"
        self.query_one("#lbl-ivs", Label).update(Text.from_markup(
            f"IVs: {count} [dim]({rate:.0f}/s)[/dim]", emoji=False
        ))

        replay_markup = (
            "[green]✓ done[/green]" if ap.wep_key is not None
            else self._replay_status_markup(campaign)
        )
        self.query_one("#lbl-replay", Label).update(
            Text.from_markup(f"Replay: {replay_markup}", emoji=False)
        )

        self._update_crack_section(ap, campaign, samples)

    def _update_crack_section(self, ap: AccessPoint, campaign, samples: int) -> None:
        """The two-row Crack section under SECURITY: a status line + a detail
        line. Only shown during a running campaign or once a key is found."""
        crack = self.query_one("#lbl-crack", Label)
        info = self.query_one("#lbl-crack-info", Label)
        persisted_wep = next((p for p in ap.persisted if p.kind == "WEP"), None)
        if campaign is None and ap.wep_key is None and persisted_wep is None:
            crack.display = False
            info.display = False
            return
        crack.display = True
        info.display = True
        target_k = CRACK_READY_THRESHOLD // 1000

        if ap.wep_key is not None:
            # Short status here — the full black-on-cyan KEY banner + copy/save
            # hint live in the (wide) EVENT LOG (a 104-bit key is too wide for
            # this column).
            crack.update(Text.from_markup(
                "Crack: [bold green]✓ Key recovered[/bold green]", emoji=False
            ))
            info.update(Text.from_markup(
                "[dim]see EVENT LOG to copy / save[/dim]", emoji=False
            ))
        elif campaign is None and persisted_wep is not None:
            # No live key/campaign, but a saved WEP key exists — mirror the
            # recovered state, tagged (history). Key chip is in the EVENT LOG.
            crack.update(Text.from_markup(
                "Crack: [bold green]✓ Key recovered[/bold green] [dim](history)[/dim]",
                emoji=False
            ))
            info.update(Text.from_markup(
                "[dim]see EVENT LOG[/dim]", emoji=False
            ))
        elif samples < CRACK_READY_THRESHOLD:
            crack.update(Text.from_markup(
                f"Crack: [white]{samples:,}/{target_k}k usable IVs[/white]",
                emoji=False
            ))
            info.update(Text.from_markup(
                f"[dim]Crack begins at {target_k}k[/dim]", emoji=False
            ))
        else:
            sc = campaign.cracker.sample_count if campaign else samples
            crack.update(Text.from_markup(
                f"Crack: [cyan]Cracking…[/cyan] [dim]({sc:,} samples)[/dim]",
                emoji=False
            ))
            info.update(Text.from_markup(
                "[dim]Some keys require >40K samples[/dim]", emoji=False
            ))

    def _update_fakeauth_line(self) -> None:
        """Render the SECURITY-panel Fake-Auth status from the running campaign."""
        fa_label = self.query_one("#lbl-fakeauth", Label)
        fa_label.display = True
        campaign = self._wep_campaign
        if campaign is None:
            fa_label.update(Text.from_markup("Fake-Auth: [dim]Off[/dim]", emoji=False))
            return
        fa = campaign.fake_auth
        if fa.state == "associated":
            countdown = ""
            if fa.next_reauth_at:
                secs = max(0, int(fa.next_reauth_at - time.time()))
                countdown = f" [dim](re-auth in {secs}s)[/dim]"
            fa_markup = f"[green]✓ Associated[/green]{countdown}"
        elif fa.state == "authenticating":
            fa_markup = "[yellow]Associating…[/yellow]"
        elif fa.state == "failed":
            fa_markup = f"[red]Failed: {escape(fa.fail_reason or 'unknown')}[/red]"
        else:
            fa_markup = "[dim]Idle[/dim]"
        fa_label.update(Text.from_markup(f"Fake-Auth: {fa_markup}", emoji=False))

    # ----- Client table ------------------------------------------------------

    def _refresh_clients(self, iface) -> None:
        ap = self.target_ap
        client_table = self.query_one("#client-table", DataTable)
        forged = iface.forged_macs
        for mac, client in iface.clients.items():
            if client.bssid != ap.bssid:
                continue
            # Skip our own forged STA(s) — fake-auth, replay source, etc. They're
            # not real clients (no more "YOU" marker; just don't list them).
            if mac in forged or client.is_self:
                continue
            if mac not in self._known_clients:
                self._known_clients.add(mac)
                client_table.add_row(
                    Text(mac),
                    Text(f"{client.signal} dBm", justify="right"),
                    Text(str(client.packets), justify="right"),
                    key=mac,
                )
            else:
                client_table.update_cell(
                    mac, "signal", Text(f"{client.signal} dBm", justify="right")
                )
                client_table.update_cell(
                    mac, "packets", Text(str(client.packets), justify="right")
                )

    _DRAGONBLOOD_GROUPS = {22, 23, 24}

    @classmethod
    def _format_sae_groups_markup(cls, ap: AccessPoint) -> str:
        """Render the Dragonblood-relevant SAE groups (22/23/24) as a compact,
        colour-coded inline list. Modern groups (19–21) are omitted — they add
        no signal to this attack and only crowd the line.

        Supported → green (the AP accepts it). Rejected → dim. No red: supporting
        one is the *finding*, not a fault, so colour mirrors the event-log tree.
        """
        parts = []
        for group in sorted(g for g in ap.sae_groups if g in cls._DRAGONBLOOD_GROUPS):
            verdict = ap.sae_groups[group]
            if verdict == "supported":
                parts.append(f"[green]{group}[/green]")
            elif verdict == "rejected":
                parts.append(f"[dim]{group}[/dim]")
        return ", ".join(parts) if parts else "[dim]—[/dim]"

    # ----- Capture-event log -------------------------------------------------

    def _drain_capture_events(self, ap: AccessPoint, forged_macs: Set[str]) -> None:
        for ev in self._events.poll(ap, forged_macs=forged_macs):
            self._log_capture_event(ev)

    def _log_capture_event(self, ev: CaptureEvent) -> None:
        client = escape(ev.client_mac)
        if ev.kind == "eapol":
            # Flat per-frame trace: one line per M1-M4 as it lands (incl. M1/M3
            # retransmits). Routine, so no solid highlight — the bg block is
            # reserved for the "Valid 4-Way Handshake" banner below. Completeness
            # is announced once per instance there, never on this line.
            msg_label = f"M{ev.msg_num}" if ev.msg_num else "EAPOL-?"
            essid = escape(ev.ssid or ev.bssid)
            self._log(
                f"[bold cyan]{msg_label}[/bold cyan] "
                f"[cyan]EAPOL handshake from [bold]{client}[/bold] "
                f"for [bold]{essid}[/bold][/cyan]"
            )
        elif ev.kind == "handshake_complete":
            # Significant event → solid highlight. Client MAC is omitted (the
            # surrounding Mx lines already carry it); re-fires per new 4-way.
            essid = escape(ev.ssid or ev.bssid)
            self._log(
                f"[black bold on green] ✓ Valid 4-Way Handshake "
                f"({ev.pair_label}) [/black bold on green] for "
                f"[bold]{essid}[/bold] [dim](press s to save)[/dim]"
            )
        elif ev.kind == "pmkid":
            # Significant event → same solid highlight as the handshake banner.
            self._log(
                f"[black bold on green] ✓ PMKID captured [/black bold on green] "
                f"from [bold]{client}[/bold] [dim](press s to save)[/dim]"
            )
        elif ev.kind == "decloak":
            method_label = DECLOAK_METHOD_LABELS.get(ev.method or "", ev.method or "?")
            self._log(
                f"[bold]Decloaked[/bold] [cyan]{escape(ev.bssid)}[/cyan] → "
                f"[green]{escape(ev.ssid or '')}[/green] "
                f"[dim]via {method_label}[/dim]"
            )

    def _log(self, markup: str) -> None:
        ts = time.strftime("%H:%M:%S")
        try:
            log = self.query_one("#focus-event-log", RichLog)
        except Exception:
            return
        log.write(Text.from_markup(f"[dim]{ts}[/dim]  {markup}", emoji=False))

    # ----- WPS PBC auto-capture ----------------------------------------------

    def _pbc_busy(self) -> bool:
        return self._pbc_task is not None and not self._pbc_task.done()

    def _other_long_running_tx(self, exclude: str = "") -> bool:
        """True if any long-running TX activity is running, EXCLUDING the named
        one (one of ``"wep"`` / ``"wpa3down"`` / ``"wps"`` / ``"pbc"``). Used to
        disable buttons for OTHER attacks so the half-duplex radio is never
        shared. Pass the running attack's name to keep its own Stop button live."""
        if exclude != "wep" and self._wep_campaign is not None:
            return True
        if exclude != "wpa3down" and self._wpa3_down_attack is not None:
            return True
        if exclude != "wps" and self._wps_campaign is not None:
            return True
        if exclude != "pbc" and self._pbc_busy():
            return True
        return False

    async def _auto_capture_pbc(self, ap: AccessPoint) -> None:
        iface = getattr(self.app, "active_interface", None)
        if not iface:
            return
        self._log("[bold cyan]WPS PushButton:[/bold cyan] [bold green]Window Open[/bold green] "
                  "— auto-capturing PSK")
        try:
            outcome = await WpsPbcCapture(
                iface, ap, log=lambda m: self._log(treelog.branch(m))
            ).capture()
            if outcome.result is PinResult.SUCCESS:
                self._pbc_done.add(ap.bssid)
                ap.wps_pbc_psk = outcome.psk
                name = escape(outcome.ssid or ap.ssid or ap.bssid)
                # Match the WPS-PIN success: green "Password for …" branch +
                # separate "(saved …)" leaf. (No cyan "PIN for …" line above —
                # PBC genuinely doesn't disclose the router's PIN; its "device
                # password" is the fixed public constant '00000000'.)
                self._log(treelog.branch(
                    f"[black bold on green] Password for {name}: "
                    f"\"{escape(outcome.psk)}\" [/black bold on green]"))
                try:
                    path = save_pbc_credential(outcome.ssid or ap.ssid or "", ap.bssid, outcome.psk)
                    self._log(treelog.leaf(f"[dim](saved {escape(path.name)})[/dim]"))
                except Exception:
                    self._log(treelog.leaf("[dim](save failed)[/dim]"))
            else:
                self._log(treelog.leaf_warn(
                    f"{outcome.result.value} [dim]({escape(outcome.detail)})[/dim] — "
                    "retrying while the window's open"))
        except Exception as exc:
            self._log(treelog.leaf_fail(f"capture error: {escape(str(exc))}"))

    def _stop_pbc_capture(self) -> None:
        """Cancel any running PBC capture + reset per-target dedup."""
        if self._pbc_task is not None:
            self._pbc_task.cancel()
            self._pbc_task = None
        self._pbc_done.clear()

    # ----- WPS PIN brute-force campaign --------------------------------------

    def _toggle_wps_pin(self) -> None:
        if self._wps_campaign is None:
            self._start_wps_pin()
        else:
            self._stop_wps_pin()
        self.update_ui()

    def _start_wps_pin(self) -> None:
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — cannot start WPS PIN.[/red]")
            return
        try:
            name = escape(ap.ssid or ap.bssid)
            # Header (tree root) for the campaign's group. The campaign's per-line
            # logs land as ├─► branches below; _stop_wps_pin closes with a ├─✓ +
            # └─✓ on success or └─► on manual stop.
            self._log(f"[bold cyan]WPS PIN brute[/bold cyan] started on [bold]{name}[/bold]")
            self._wps_campaign = WpsCampaign(
                iface, ap, log=lambda m: self._log(treelog.branch(m)))
            self._wps_campaign.start()
        except Exception as exc:
            logger.exception("WPS PIN start failed")
            self._log(f"[bold red]✗ WPS PIN failed to start:[/bold red] {escape(str(exc))}")
            self._wps_campaign = None

    def _stop_wps_pin(self) -> None:
        if self._wps_campaign is None:
            return
        camp = self._wps_campaign
        # stop() is async; fire-and-forget (the campaign tears down its own
        # transport/association in _teardown so this is safe).
        asyncio.create_task(camp.stop())
        self._wps_campaign = None
        ssid = escape(camp.target.ssid or camp.bssid)
        if camp.state.found_pin:
            self._log(treelog.branch_ok(
                f"[black bold on cyan]  WPS PIN for {ssid}: "
                f"{escape(camp.state.found_pin)}  [/black bold on cyan]"))
            self._log(treelog.branch(
                f"[black bold on green] Password for {ssid}: "
                f"\"{escape(camp.state.found_psk or '')}\" [/black bold on green]"))
            # Persist the recovered creds so they survive exit + light the
            # Scanner badge on next start (same .wps file as PBC, method tag
            # discriminates). The saved path is its own └─► leaf below the
            # Password branch so the tree closes cleanly.
            try:
                path = save_pbc_credential(
                    camp.target.ssid or "", camp.bssid, camp.state.found_psk or "",
                    method="WPS-PIN", pin=camp.state.found_pin)
                self._log(treelog.leaf(f"[dim](saved {escape(path.name)})[/dim]"))
            except Exception:
                self._log(treelog.leaf("[dim](save failed)[/dim]"))
        else:
            self._log(treelog.leaf(
                f"[yellow]WPS PIN stopped[/yellow] "
                f"[dim]({camp.state.tested} tested, phase {camp.state.phase})[/dim]"))

    @staticmethod
    def _fmt_eta(secs: Optional[float]) -> str:
        if secs is None:
            return "?"
        if secs < 60:
            return f"{int(secs)}s"
        if secs < 3600:
            return f"{int(secs / 60)}m"
        return f"{secs / 3600:.1f}h"

    @staticmethod
    def _compact_count(n: int) -> str:
        """Width-bounded counter — keeps `tested` to ≤4 chars so the narrow
        SECURITY row never truncates: 0..999 verbatim, then 1.5k / 15k."""
        if n < 1000:
            return str(n)
        if n < 10000:
            return f"{n / 1000:.1f}k"        # 1500 → "1.5k"
        return f"{n // 1000}k"               # 15000 → "15k"

    def _wps_status_markup(self, camp: WpsCampaign) -> str:
        """Compact campaign status for the dedicated lbl-wps-status row (~29
        chars before the SECURITY panel truncates). The static WPS/PMF row
        carries the beacon-level 🔒 (the "hard lock"); this row carries the
        live PIN-campaign progress and our internal soft/hard backoff state."""
        st = camp.state
        if st.found_pin:
            return (f"[black bold on cyan] PIN CRACKED: ✓ "
                    f"{escape(st.found_pin)} [/black bold on cyan]")
        tested = self._compact_count(st.tested)
        if camp.status == "locked":
            # Countdown updates each tick (10 Hz); kind disambiguates the 🔒
            # in the row above (hard = AP says no; soft = our own backoff).
            remaining = int(camp.lock_remaining_seconds)
            m, s = divmod(remaining, 60)
            countdown = f"{m}:{s:02d}"
            kind = camp.lock_kind or "soft"
            color = "red" if kind == "hard" else "dark_orange"
            return (f"WPS PIN: [cyan]{tested}[/cyan]/11k · "
                    f"[{color}]{kind} {countdown}[/{color}]")
        if camp.status in ("failed", "error"):
            return f"WPS PIN: [red]{camp.status}[/red] [dim]({tested}/11k)[/dim]"
        eta = self._fmt_eta(camp.eta_seconds)
        if st.phase == "second_half" and st.first_half:
            # First half is locked in — the meaningful keyspace is the second
            # half (1k candidates), so the denominator narrows from 11k to 1k.
            # p2_index = "how many second-half candidates we've burned through."
            return (f"WPS PIN: [cyan]{st.p2_index}[/cyan]/1k · "
                    f"[green]p1={escape(st.first_half)}[/green] [dim]{eta}[/dim]")
        return f"WPS PIN: [cyan]{tested}[/cyan]/11k · [dim]ETA {eta}[/dim]"

    # ----- Actions / handlers ------------------------------------------------

    def _cursor_mac(self) -> Optional[str]:
        """MAC of the highlighted CLIENTS row (cursor selection — no checkboxes),
        or None when the table is empty. Read straight from the table so it's
        never stale."""
        t = self.query_one("#client-table", DataTable)
        if t.row_count == 0:
            return None
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-save":
            self._save_capture()
        elif bid == "btn-deauth-bcast":
            self.run_worker(self._run_deauth_broadcast(), exclusive=True)
        elif bid == "btn-deauth-sel":
            self.run_worker(self._run_deauth_selected(), exclusive=True)
        elif bid == "btn-pmkid":
            self.run_worker(self._run_pmkid_harvest(), exclusive=True)
        elif bid == "btn-wps-pin":
            self._toggle_wps_pin()
        elif bid == "btn-wpa3-down":
            self._toggle_wpa3_down()
        elif bid == "btn-gen-ivs":
            self._toggle_generate_ivs()
        elif bid == "btn-frag":
            self._toggle_frag()
        elif bid == "btn-chop":
            self._toggle_chop()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Show/hide the Save + Copy footer keys based on the target's
        encryption and whether there's anything to save yet. Returning False
        hides the binding entirely (and blocks the keypress); the rest stay
        shown. ``update_ui`` calls ``refresh_bindings()`` so this re-evaluates
        as the campaign progresses."""
        if action in ("save_capture", "save_key", "copy_key"):
            ap = self.target_ap
            if ap is None:
                return False
            is_wep = (ap.encryption or "").upper() == "WEP"
            if action == "save_capture":      # WPA: a handshake/PMKID to write
                return not is_wep and ap.has_capture
            # save_key / copy_key: a recovered WEP key
            return is_wep and ap.wep_key is not None
        return True

    def action_save_capture(self) -> None:
        self._save_capture()

    def action_save_key(self) -> None:
        # Same writer as Save Capture — it routes to the WEP-key path when a key
        # is present; the separate action just gives the footer a WEP label.
        self._save_capture()

    def action_copy_key(self) -> None:
        """Copy the recovered WEP key (hex) to the clipboard — saves the user
        from hand-selecting terminal text. No-op with a hint if not cracked."""
        # Read from the AP (persists after the finished campaign is torn down).
        key = self.target_ap.wep_key if self.target_ap else None
        if not key:
            self._log("[yellow]No WEP key recovered yet — nothing to copy.[/yellow]")
            return
        kh = key.hex()
        try:
            self.app.copy_to_clipboard(kh)
            self._log(f"[green]✓ Copied WEP key to clipboard:[/green] [bold]{kh}[/bold]")
        except Exception:
            # Clipboard may be unavailable (no OSC-52 support); the key is
            # still right there in the log/panel to select manually.
            self._log(f"[yellow]Clipboard unavailable — key is:[/yellow] [bold]{kh}[/bold]")

    async def _run_deauth_broadcast(self) -> None:
        """Worker: broadcast-deauth every station associated with the focused AP."""
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — aborting Broadcast.[/red]")
            return

        BROADCAST = "ff:ff:ff:ff:ff:ff"
        # deauth(burst_count=N) sends 2 frames per round (one to the dest, one
        # to the AP); broadcast uses the default 10 rounds → 20 frames.
        self._log(
            f"[bold]Broadcast de-auth[/bold] on "
            f"[bold]{escape(ap.ssid or '<hidden>')}[/bold] · CH {ap.channel}"
        )
        try:
            await iface.deauth(ap.bssid, BROADCAST)
        except Exception as exc:
            logger.exception("Broadcast deauth crashed")
            self._log(treelog.leaf_fail(f"Broadcast failed: {escape(str(exc))}"))
            return
        self._log(treelog.leaf_ok(
            "[bold green]Sent 20 broadcast deauth frames[/bold green] [dim](10 bursts)[/dim]"
        ))

    # Total deauth pairs per selected client. Round-robin'd across clients so
    # each frame pair is followed by a 10ms RX window — keeps the radio from
    # being TX-saturated when many clients are queued (wifite2 starved RX
    # exactly this way when too many deauths were back-to-back).
    _DEAUTH_SEL_ROUNDS = 10

    async def _run_deauth_selected(self) -> None:
        """Worker: deauth the highlighted client (cursor row)."""
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — aborting Deauth.[/red]")
            return
        mac = self._cursor_mac()
        if not mac:
            self._log("[yellow]⚠ No client highlighted — pick a row first.[/yellow]")
            return

        self._log(
            f"[bold]De-authenticating[/bold] [bold]{escape(mac)}[/bold] "
            f"on [bold]{escape(ap.ssid or '<hidden>')}[/bold] · CH {ap.channel}"
        )
        try:
            for _ in range(self._DEAUTH_SEL_ROUNDS):
                await iface.deauth(ap.bssid, mac, burst_count=1)
        except Exception as exc:
            logger.exception("Deauth %s crashed", mac)
            self._log(treelog.leaf_fail(f"Deauth failed: {escape(str(exc))}"))
            return
        # 10 rounds × burst_count=1, 2 frames/round → 20 frames (client + AP).
        self._log(treelog.leaf_ok(
            "[bold green]Sent 20 deauth frames[/bold green] [dim](10× client + AP)[/dim]"
        ))

    async def _run_pmkid_harvest(self) -> None:
        """Worker: run a PMKID harvest against the focused AP."""
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — aborting PMKID harvest.[/red]")
            return

        self._log(
            f"[bold cyan]Harvesting PMKID[/bold cyan] from "
            f"[bold]{escape(ap.ssid or '<hidden>')}[/bold]…"
        )
        attack = PmkidHarvestAttack(iface, ap)
        try:
            pmkid = await attack.run()
        except Exception as exc:
            logger.exception("PMKID harvest crashed")
            self._log(treelog.leaf_fail(f"PMKID harvest crashed: {escape(str(exc))}"))
            return

        if pmkid:
            self._log(treelog.branch_ok(
                f"[bold green]PMKID harvested:[/bold green] "
                f"[black bold on cyan] {pmkid.hex()} [/black bold on cyan]"
            ))
            self._log(treelog.leaf(
                "[dim]press[/] [cyan bold]s[/] [dim]to[/] [cyan bold]save[/] [dim]the hashline[/]"
            ))
        else:
            self._log(treelog.branch_fail("[bold red]No PMKID harvested[/bold red] — possible reasons:"))
            self._log(treelog.branch("[dim]AP may not advertise a PMKID KDE[/dim]"))
            self._log(treelog.leaf("[dim]PMF / status rejected the request[/dim]"))

    async def _run_sae_probe(self) -> None:
        """Worker: enumerate which SAE groups the focused AP accepts."""
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — aborting SAE probe.[/red]")
            return

        self._log(
            f"[bold cyan]Probing SAE groups[/bold cyan] on "
            f"[bold]{escape(ap.ssid or '<hidden>')}[/bold]…"
        )
        attack = SAEGroupProbeAttack(iface, ap)
        try:
            results = await attack.run()
        except Exception as exc:
            logger.exception("SAE probe crashed")
            self._log(treelog.leaf_fail(f"SAE probe crashed: {escape(str(exc))}"))
            return

        # Only the Dragonblood-relevant legacy FFC groups matter here (22/23/24);
        # the modern groups feed the panel's SAE-Groups line but add no signal to
        # this verdict. List = green when the AP supports the group, dim when it
        # doesn't — no red (supporting one is the *finding*, not a fault).
        DRAGONBLOOD = (22, 23, 24)
        supported_groups = [g for g, (lbl, _) in results.items() if lbl == "Supported"]
        dragonblood_hits = [g for g in DRAGONBLOOD if g in supported_groups]
        for group in DRAGONBLOOD:
            entry = results.get(group)
            if entry and entry[0] == "Supported":
                self._log(treelog.branch_ok(
                    f"[green]Group {group}: supported[/green] [dim]— {escape(entry[1])}[/dim]"
                ))
            elif entry and entry[0] == "Rejected":
                self._log(treelog.branch_dim(f"Group {group}: not supported"))
            else:
                self._log(treelog.branch_dim(f"Group {group}: no definitive answer"))

        # Verdict polarity is the auditor's: Dragonblood-vulnerable is the win
        # (green), not-vulnerable closes with a red ╳, indeterminate with ⚠.
        if dragonblood_hits:
            hits = ", ".join(str(g) for g in dragonblood_hits)
            self._log(treelog.branch_ok(
                f"[green]Vulnerable to Dragonblood[/green] "
                f"[dim](group{'s' if len(dragonblood_hits) > 1 else ''} {hits} "
                f"— CVE-2019-9494/9495)[/dim]"
            ))
            self._log(treelog.leaf(
                "[dim]Next: capture a handshake, then run "
                "[bold]dragonblood-tools[/bold] to recover the passphrase[/dim]"
            ))
        elif supported_groups:
            self._log(treelog.leaf_fail("[bold red]Not vulnerable to Dragonblood[/bold red]"))
        else:
            self._log(treelog.leaf_warn(
                "Couldn't determine SAE groups — rate-limited, PMF, or off-channel"
            ))

    def _toggle_wpa3_down(self) -> None:
        """Button handler: Start the spoof daemon if idle, Stop it if running."""
        if self._wpa3_down_attack:
            self._stop_wpa3_down()
        else:
            self._start_wpa3_down()

    def _start_wpa3_down(self) -> None:
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — cannot start WPA3 Down.[/red]")
            return
        if not ap.ssid:
            self._log(
                "[yellow]⚠ Cannot run WPA3 Down on a hidden AP — "
                "SSID unknown, no probe-response payload to forge.[/yellow]"
            )
            return
        if not ap.transition_mode:
            self._log(
                "[yellow]⚠ Target is pure WPA3 (no WPA2 fallback advertised) — "
                "downgrade not possible.[/yellow]"
            )
            return
        try:
            # No per-probe log callback — live counts go to the SECURITY panel
            # (see _render_wpa3_down) so the event log isn't flooded and stays
            # tree-clean even while Deauth etc. interleave. Per-probe detail is
            # in the debug logger.
            self._wpa3_down_attack = WPA3DowngradeAttack(iface, ap)
            self._wpa3_down_attack.start()
        except Exception as exc:
            logger.exception("WPA3 Down start failed")
            self._log(f"[bold red]✗ WPA3 Down failed to start:[/bold red] {escape(str(exc))}")
            self._wpa3_down_attack = None
            return
        self._log(
            f"[bold cyan]WPA3 Downgrade ACTIVE[/bold cyan] on [bold]{escape(ap.ssid)}[/bold]"
        )
        self._log(treelog.branch(
            "[dim]responding to probe requests with[/dim] [white bold]WPA2-only[/white bold]"
        ))
        self._log(treelog.leaf("[dim](reconnects take minutes–hours)[/dim]"))

    def _toggle_generate_ivs(self) -> None:
        """Button handler. Running campaign → stop. No campaign (incl. one that
        already finished and was torn down) → start fresh."""
        camp = self._wep_campaign
        if camp is not None and camp.recovered_key is None:
            self._stop_generate_ivs()
        else:
            if camp is not None:      # a finished campaign still around — clear it
                self._stop_generate_ivs()
            self._start_generate_ivs()

    def _start_generate_ivs(self) -> None:
        ap = self.target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — cannot Generate IVs.[/red]")
            return
        try:
            self._wep_campaign = WepCampaign(iface, ap, log_callback=self._log)
            self._wep_campaign.start()
        except Exception as exc:
            logger.exception("Generate IVs start failed")
            self._log(f"[bold red]✗ Generate IVs failed to start:[/bold red] {escape(str(exc))}")
            self._wep_campaign = None

    def _stop_generate_ivs(self) -> None:
        if not self._wep_campaign:
            return
        you_mac = ":".join(
            f"{b:02x}" for b in self._wep_campaign.fake_auth.source_mac
        )
        self._wep_campaign.stop()
        self._wep_campaign = None
        # The campaign dropped our STA from iface.clients; clear its table row
        # so it doesn't linger as a stale target.
        self._known_clients.discard(you_mac)
        try:
            self.query_one("#client-table", DataTable).remove_row(you_mac)
        except Exception:
            pass

    def _toggle_frag(self) -> None:
        """Frag button. It's a sub-mode of a running campaign — switches the
        radio from ARP replay to fragmentation (and back). Click-to-toggle."""
        camp = self._wep_campaign
        if camp is None:
            self._log(
                "[yellow]Start Generate IVs first[/yellow] [dim](Frag "
                "manufactures an ARP seed for the replay engine)[/dim]"
            )
            return
        if camp.frag_active:
            camp.stop_frag()
            self._log("[cyan]→ Frag stopped[/cyan] [dim](back to ARP replay)[/dim]")
        else:
            # The daemon logs its own one-liner (waiting → seeded → worked); no
            # need for a separate start line back-to-back with it.
            camp.start_frag()
        self.update_ui()

    def _toggle_chop(self) -> None:
        """Chop button — sibling of Frag (mutually exclusive). Byte-by-byte
        ICV-oracle decryption to forge a seed when frag gets no response."""
        camp = self._wep_campaign
        if camp is None:
            self._log(
                "[yellow]Start Generate IVs first[/yellow] [dim](ChopChop "
                "manufactures an ARP seed for the replay engine)[/dim]"
            )
            return
        if camp.chop_active:
            camp.stop_chop()
            self._log("[cyan]→ Chop stopped[/cyan] [dim](back to ARP replay)[/dim]")
        else:
            # The daemon logs its own tree (waiting → chopping IV → steps →
            # worked/gave-up); no separate start line back-to-back with it.
            camp.start_chop()
        self.update_ui()

    def _stop_wpa3_down(self) -> None:
        if not self._wpa3_down_attack:
            return
        stats = self._wpa3_down_attack.stop()
        self._wpa3_down_attack = None
        duration = max(1, int(time.time() - stats.started_at))
        failed = f" ({stats.responses_failed} failed)" if stats.responses_failed else ""
        self._log("[bold red]WPA3 Downgrade stopped[/bold red]")
        self._log(treelog.branch(
            f"[dim]Sent {stats.responses_sent} probe responses in "
            f"{_format_duration(duration)}{failed}[/dim]"
        ))
        self._log(treelog.leaf(
            f"[dim]Probe requests: {stats.directed_probes} directed, "
            f"{stats.wildcard_probes} wildcard[/dim]"
        ))

    async def action_go_back(self) -> None:
        # Tear down the WPA3 Down daemon if running — Scanner view doesn't own
        # the AP's channel and the RX callback would keep firing forever.
        self._stop_wpa3_down()
        # Same for the Generate IVs campaign — stop injecting + drop the YOU
        # client on exit.
        self._stop_generate_ivs()
        self._stop_pbc_capture()
        self._stop_wps_pin()
        # Hopper restart happens in ScannerView.on_screen_resume — it owns
        # the channel filter; restarting here would silently widen it.
        self.app.pop_screen()

    # ----- Save --------------------------------------------------------------

    def _save_capture(self) -> None:
        ap = self.target_ap
        if not ap or not ap.has_capture:
            self._log("[yellow]⚠ Nothing to save yet.[/yellow]")
            return

        # WEP: there's no handshake/pcap — just write the recovered key.
        if ap.wep_key is not None:
            self._save_wep_key(ap)
            return

        frames: list[bytes] = []
        beacon_added = False
        for hs in ap.handshakes.values():
            if hs.beacon_frame and not beacon_added:
                frames.append(hs.beacon_frame)
                beacon_added = True
            for f in hs.eapol_frames:
                frames.append(f.raw)

        # Count distinct handshake instances (matches the CAPTURE panel and the
        # one-WPA*02-line-per-instance the hc22000 writer now emits).
        n_complete = sum(hs.complete_instances for hs in ap.handshakes.values())
        n_pmkid = sum(1 for hs in ap.handshakes.values() if hs.pmkid)

        captures_dir = Path("captures")
        safe_ssid = re.sub(r"[^A-Za-z0-9_-]", "_", ap.ssid or "hidden")[:24] or "hidden"
        safe_bssid = ap.bssid.replace(":", "-")
        stem = f"{safe_ssid}_{safe_bssid}_{int(time.time())}"
        pcap_path = captures_dir / f"{stem}.pcap"
        hc_path = captures_dir / f"{stem}.hc22000"

        try:
            n_written = write_pcap(pcap_path, frames)
            n_hashlines = write_hc22000(hc_path, ap)
        except Exception as exc:
            logger.exception("Save capture failed")
            self._log(f"[bold red]✗ Save failed:[/bold red] {escape(str(exc))}")
            return

        parts: list[str] = []
        if n_complete:
            parts.append(f"{n_complete} handshake(s)")
        if n_pmkid:
            parts.append(f"{n_pmkid} PMKID(s)")
        summary = " + ".join(parts) if parts else f"{n_written} frame(s)"
        self._log(
            f"[bold green]✓ Saved {summary}[/bold green] "
            f"({n_written} frames) → [bold]{escape(str(pcap_path))}[/bold]"
        )
        if n_hashlines:
            self._log(
                f"[bold green]✓ {n_hashlines} hashline(s)[/bold green] "
                f"→ [bold]{escape(str(hc_path))}[/bold] "
                f"[dim](hashcat -m 22000)[/dim]"
            )
        else:
            self._log(
                "[dim]  (no hc22000 hashline produced — "
                "hidden SSID or truncated capture)[/dim]"
            )

    def _save_wep_key(self, ap: AccessPoint) -> None:
        """Write the recovered WEP key to captures/<ssid>_<bssid>_<ts>_wepkey.txt."""
        key = ap.wep_key
        captures_dir = Path("captures")
        safe_ssid = re.sub(r"[^A-Za-z0-9_-]", "_", ap.ssid or "hidden")[:24] or "hidden"
        safe_bssid = ap.bssid.replace(":", "-")
        path = captures_dir / f"{safe_ssid}_{safe_bssid}_{int(time.time())}_wepkey.txt"
        ascii_form = (
            key.decode("ascii") if all(0x20 <= b < 0x7F for b in key) else ""
        )
        try:
            captures_dir.mkdir(parents=True, exist_ok=True)
            lines = [
                f"SSID:  {ap.ssid or '<hidden>'}",
                f"BSSID: {ap.bssid}",
                f"WEP key (hex):   {key.hex()}",
            ]
            if ascii_form:
                lines.append(f'WEP key (ASCII): "{ascii_form}"')
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as exc:
            logger.exception("Save WEP key failed")
            self._log(f"[bold red]✗ Save failed:[/bold red] {escape(str(exc))}")
            return
        self._log(
            f"[bold green]✓ Saved WEP key[/bold green] → [bold]{escape(str(path))}[/bold]"
        )
