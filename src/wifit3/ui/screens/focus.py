import asyncio
import logging
import math
import time
from collections import deque
from datetime import datetime
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
from wifit3.engine.save import (
    save_handshake, save_pmkid, save_wep_key, save_wps_pbc, save_wps_pin,
)
from wifit3.engine.attacks.pmkid_harvest import PmkidHarvestAttack
from wifit3.engine.attacks.wpa3_downgrade import WPA3DowngradeAttack
from wifit3.engine.attacks.wep.campaign import WepCampaign
from wifit3.engine.attacks.wps.campaign import WpsCampaign
from wifit3.engine.attacks.wps.pbc import WpsPbcCapture
from wifit3.engine.attacks.wps.registrar import PinResult

from .. import focus_model as fm
from ..capture_events import DECLOAK_METHOD_LABELS, CaptureEvent, CaptureEventDetector, CaptureKind
from ..capture_log import eapol_message_markup, short_sta
from ..widgets.packet_dashboard import PacketDashboard
from ..signal_bar import render_signal_bar
from ..encryption_format import (
    format_encryption_markup,
    wep_key_ascii,
)

logger = logging.getLogger(__name__)

# Signal bar: 16 cells, right-pinned in the CAPTURE panel. The panel is
# `width: 38` (app.py CSS); minus its border (2) + padding (2) leaves a 34-cell
# content area to pin the bar's right edge against.
SIGNAL_BAR_WIDTH = 16
_CAPTURE_CONTENT_W = 34


def _wep_key_chip(key_hex: Optional[str]) -> str:
    """Black-bold-on-cyan WEP key chip wrapping the shared `<hex> = "ascii"`
    display form (bare hex for non-printable / binary keys)."""
    if not key_hex:
        return "[dim]?[/dim]"
    return f"[black bold on cyan] {wep_key_ascii(key_hex)} [/black bold on cyan]"


class FocusView(Screen):
    """The Attack/Focus mode for a specific AP."""

    BINDINGS = [
        Binding("escape", "go_back", "Back to Scanner", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

    def __init__(self):
        super().__init__()
        self.target_ap: AccessPoint = None
        self._refresh_timer = None
        self._known_clients: Set[str] = set()
        # Sliding-window samples of (timestamp, ap.beacons) for a recent
        # beacons/s rate instead of a since-first-seen average (see update_ui).
        self._beacon_samples: deque = deque()
        # Signal bar: update_ui (10 Hz) sets the target rate + count; a 30 FPS
        # timer (_animate_signal) eases _sig_display toward it and owns the render.
        self._sig_target: Optional[float] = None
        self._sig_display: float = 0.0
        self._sig_count: int = 0
        self._sig_timer = None
        self._lbl_beacons: Optional[Label] = None
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
        # clearly a target of interest). One task at a time (_pbc_task); re-invade
        # is gated by ap.has_psk, which persists across target switches + restarts.
        self._pbc_task: Optional[asyncio.Task] = None

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
                    # ATTACKS — no title bar: buttons self-label, family stated
                    # by TARGET's "Encryption:" line. WEP and WPA buttons share
                    # the rows; update_ui shows only the set that fits the
                    # target:
                    #   WEP:  Replay  Chop        WPA:  PMKID  WPS PIN
                    #                                   WPA ↓
                    with Vertical(classes="info-box", id="attack-panel"):
                        with Horizontal(classes="button-row"):
                            yield Button("Replay", variant="success", id="btn-gen-ivs")
                            yield Button("Chop", variant="primary", id="btn-chop")
                            yield Button("PMKID", variant="primary", id="btn-pmkid")
                            yield Button("WPS PIN", variant="primary", id="btn-wps-pin", disabled=True)
                        with Horizontal(classes="button-row"):
                            yield Button("WPA ↓", variant="primary", id="btn-wpa3-down", disabled=True)
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
                        # Live packet dashboard — fills the dead space right of
                        # CAPTURE on a wide terminal (collapses on a narrow one).
                        # Wrapped like SECURITY/CAPTURE so its title bar matches.
                        with Vertical(classes="info-box", id="panel-activity-box"):
                            yield Label("PACKET ACTIVITY", classes="panel-title")
                            yield PacketDashboard(id="panel-activity")
                    # The stretchy panel — fills the rest of the right column.
                    with Vertical(classes="info-box", id="event-log-panel"):
                        yield Label("EVENT LOG", classes="panel-title")
                        yield RichLog(id="focus-event-log", markup=True, highlight=False, wrap=True)

        yield Footer()

    async def on_mount(self) -> None:
        self._refresh_timer = self.set_interval(1 / 10, self.update_ui)
        self._lbl_beacons = self.query_one("#lbl-beacons", Label)
        self._sig_timer = self.set_interval(1 / 30, self._animate_signal)
        await self._init_target()

    def _animate_signal(self) -> None:
        """30 FPS: ease the signal bar toward the windowed beacons/s and drive
        the dead-AP heartbeat — independent of the 10 Hz whole-view refresh, so
        the bar glides between data updates instead of stepping."""
        if self._lbl_beacons is None:
            return
        target = self._sig_target
        if target is None:
            bar = render_signal_bar(None, width=SIGNAL_BAR_WIDTH)
        elif target <= 0.05:
            # Dead: a smooth 1 s sine heartbeat (pulse 0..1).
            pulse = 0.5 + 0.5 * math.sin(time.time() * math.tau)
            bar = render_signal_bar(0.0, width=SIGNAL_BAR_WIDTH, pulse=pulse)
            self._sig_display = 0.0
        else:
            # Ease ~0.2 s toward the target so rate jumps glide.
            self._sig_display += (target - self._sig_display) * 0.25
            bar = render_signal_bar(self._sig_display, width=SIGNAL_BAR_WIDTH)
        left = Text("Beacons: ", no_wrap=True)
        left.append(f"{self._sig_count:,}", style="bold")
        pad = max(1, _CAPTURE_CONTENT_W - left.cell_len - bar.cell_len)
        line = Text(no_wrap=True)
        line.append_text(left)
        line.append(" " * pad)
        line.append_text(bar)
        self._lbl_beacons.update(line)

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
        self._sig_target = None
        self._sig_display = 0.0
        self._sig_count = 0
        self._events.reset()
        self.query_one("#client-table", DataTable).clear()
        self.query_one("#focus-event-log", RichLog).clear()

        if self.target_ap.ssid:
            chip = f"[black bold on cyan] {escape(self.target_ap.ssid)} [/black bold on cyan]"
            self._log(f"[bold]Target acquired:[/bold] {chip}")
        else:
            self._log("[bold]Target acquired:[/bold] "
                      "[dim italic]cloaked network — hidden SSID[/dim italic]")

        # BSSID is the └─► terminal when there's no interface to tune; otherwise
        # it's a ├─► branch and the tune line closes the group. Keeps the tree
        # leaf-terminated on both paths (RichLog is append-only — no rewrites).
        iface = getattr(self.app, "active_interface", None)
        # Point the packet dashboard at this target (clears its per-class
        # windows so they never bleed across targets). Idle/dim if no iface.
        # WEP-IV row is for WEP targets, EAPOL row for WPA/WPA2/WPA3 (anything
        # that isn't WEP or OPEN); update_ui re-evaluates this live in case the
        # encryption label upgrades after focus.
        enc = (self.target_ap.encryption or "").upper()
        self.query_one("#panel-activity", PacketDashboard).focus_on(
            iface, self.target_ap.bssid,
            show_wep=enc == "WEP",
            show_eapol=enc not in ("WEP", "OPEN", ""),
        )
        bssid_line = f"[dim]BSSID:[/dim] [white]{self.target_ap.bssid}[/white]"
        if iface:
            self._log(treelog.branch(bssid_line))
            ok = await iface.set_channel(self.target_ap.channel, scan=False)
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
            self._log("[bold yellow]PMF Required:[/] "
                "AP requires [bold]Protected Management Frames[/]")
            self._log(treelog.leaf("[italic]Deauth[/] attacks have been disabled"))

        self._log_persisted_history(self.target_ap)
        # Make it obvious the view is now primed and passively capturing.
        if enc == "WEP":
            self._log("[green]●[/green] Listening for [bold]WEP IVs[/bold]…")
        elif enc == "WPA3 ":
            pass
        elif enc not in ("OPEN", ""):
            self._log("[green]●[/green] Listening for [bold]handshake[/bold] + PMKID…")
        self.update_ui()

    def _log_persisted_history(self, ap: AccessPoint) -> None:
        """On target acquisition, surface saved captures/ artifacts for this AP
        so the user knows exactly what's already on disk (and how stale) — the
        same data that lights the Scanner badges."""
        if not ap.persisted:
            return
        newest_first = sorted(ap.persisted, key=lambda c: c.timestamp, reverse=True)
        by_kind: dict[str, list] = {}
        for cap in newest_first:
            by_kind.setdefault(cap.kind, []).append(cap)

        # Summary line, e.g. "Existing captures in captures/ — 1 handshake, 6 PMKIDs:"
        nouns = {"HS": "handshake", "PMKID": "PMKID", "WEP": "WEP key", "WPS": "WPS PSK"}
        parts = [
            f"[bold]{len(by_kind[k])}[/bold] {nouns[k]}{'s' if len(by_kind[k]) != 1 else ''}"
            for k in ("HS", "PMKID", "WEP", "WPS") if k in by_kind
        ]
        self._log(f"[bold]Existing captures[/bold] in [cyan]captures/[/cyan] — "
                  f"{', '.join(parts)}:")

        # Newest of each kind, then a single "(+N older)" leaf — so an AP with 20
        # PMKIDs lists one row, not twenty.
        shown = sorted((caps[0] for caps in by_kind.values()),
                       key=lambda c: c.timestamp, reverse=True)
        older = len(ap.persisted) - len(shown)
        for i, cap in enumerate(shown):
            is_last = i == len(shown) - 1 and older == 0
            line = treelog.leaf if is_last else treelog.branch
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
        if older:
            self._log(treelog.leaf(
                f"[dim](+{older} older capture{'s' if older != 1 else ''})[/dim]"))

    # ----- Per-tick UI refresh -----------------------------------------------

    def update_ui(self) -> None:
        if not self.target_ap or not self.is_current:
            return

        ap = self.target_ap
        enc = (ap.encryption or "").upper()
        is_wep = enc == "WEP"

        # Keep the packet-dashboard's encryption-gated rows in sync — the label
        # can upgrade after focus (provisional WEP → WPA2 on a weak radio).
        # Cheap: a no-op unless the gates actually changed.
        self.query_one("#panel-activity", PacketDashboard).set_gates(
            show_wep=is_wep, show_eapol=enc not in ("WEP", "OPEN", ""),
        )

        # Opportunistic WPS PBC: if a push-button window opens on this target,
        # auto-capture the PSK. We're already on-channel; gated to one attempt at
        # a time, once per BSSID, and only when no other TX activity owns the radio.
        if (ap.wps_pbc_active and not self._pbc_busy()
                and not ap.has_psk
                and self._wep_campaign is None and self._wpa3_down_attack is None
                and self._wps_campaign is None):
            self._pbc_task = asyncio.create_task(self._auto_capture_pbc(ap))

        # TARGET INFO panel. SSID as a chip (no "ESSID:" prefix) so short names
        # like "NETGEAR" are still visible; truncated with … to fit the panel.
        self.query_one("#lbl-ssid", Label).update(
            Text.from_markup(fm.ssid_chip_markup(ap), emoji=False)
        )
        self.query_one("#lbl-bssid", Label).update(
            Text.from_markup(f"BSSID: [bold]{ap.bssid}[/bold]", emoji=False)
        )
        # In FocusView the hopper is stopped and the channel is pinned to this
        # target — that's implicit, so we don't label it (a "(Locked)" tag just
        # reads as something being wrong).
        self.query_one("#lbl-channel", Label).update(Text(f"Channel: {ap.channel}"))
        # Last Beacon doubles as a "is the card actually on-channel?" health
        # readout: an active AP sits at "now", so any drift is the tell that we've
        # stopped hearing it (mistune / wedged RX). Escalate hard — a coloured
        # chip by 1s, red by 3s — so a deaf card can't be missed.
        self.query_one("#lbl-last-beacon", Label).update(
            Text.from_markup(
                f"Last Beacon: {fm.last_beacon_markup(ap, time.time())}", emoji=False)
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
        wps_pmf = fm.wps_pmf_markup(ap)
        if wps_pmf:
            wps_label.display = True
            wps_label.update(Text.from_markup(wps_pmf, emoji=False))
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
                fm.wps_status_markup(self._wps_campaign), emoji=False))
        else:
            wps_status_label.display = False
        self.query_one("#lbl-pmf", Label).display = False

        # WPA3-mode line dropped — redundant with the Encryption line, which
        # already states e.g. "WPA3→2 (transition)".
        self.query_one("#lbl-wpa3", Label).display = False

        # WPA3-Down live status — shown only while the daemon is running. This
        # is where the per-probe counts surface (instead of flooding the log).
        down_label = self.query_one("#lbl-wpa3-down", Label)
        wpa3_down_line = fm.wpa3_down_markup(self._wpa3_down_attack)
        if wpa3_down_line:
            down_label.display = True
            down_label.update(Text.from_markup(wpa3_down_line, emoji=False))
        else:
            down_label.display = False

        # Attack buttons. WEP and WPA targets get disjoint button sets — the
        # WPA buttons (PMKID/WPA3 Down) are meaningless for WEP, so hide (not
        # just disable) them and surface Fake-Auth in their place. The per-button
        # visibility + enablement + label/variant is derived in focus_model
        # (shared with v2); only the side effect of tearing down a finished WEP
        # campaign stays here.
        if is_wep:
            # A finished campaign (key recovered) is torn down so Replay reverts
            # from "Stop Replay" — the user shouldn't have to click Stop after a
            # successful crack. The branch may fire on several update_ui ticks
            # before _stop_generate_ivs nulls the campaign; save_wep_key reports
            # was_new=False on the second+ pass and we surface the path either
            # way so the user always sees where the key landed (it persists on
            # ap.wep_key). Done BEFORE derive_buttons so it sees the nulled
            # campaign and renders the start ("Replay") state.
            camp = self._wep_campaign
            if camp is not None and camp.recovered_key is not None:
                result = save_wep_key(ap, camp.recovered_key)
                if result is not None:
                    verb = "saved" if result.was_new else "already saved as"
                    self._log(f"[dim]({verb} {escape(result.path.name)})[/dim]")
                self._stop_generate_ivs()

        btns = fm.derive_buttons(ap, self._campaigns())
        self._apply_button("#btn-gen-ivs", btns.gen_ivs)
        self._apply_button("#btn-chop", btns.chop)
        self._apply_button("#btn-pmkid", btns.pmkid)
        self._apply_button("#btn-wps-pin", btns.wps_pin)
        self._apply_button("#btn-wpa3-down", btns.wpa3_down)

        if is_wep:
            self._update_fakeauth_line()
        else:
            self.query_one("#lbl-fakeauth", Label).display = False
            self.query_one("#lbl-crack", Label).display = False
            self.query_one("#lbl-crack-info", Label).display = False

        # CAPTURE panel (dynamic). Beacon RATE is windowed over the last few
        # seconds, not averaged since first_seen — an average converges to a
        # flat line and hides how RX is doing *right now* (e.g. you moved, or
        # the AP got busy). Total count stays cumulative. The windowed rate +
        # count feed the 30 FPS signal-bar animator (_animate_signal owns the
        # lbl-beacons render so the bar eases/pulses between these 10 Hz ticks).
        self._sig_target, self._sig_count = fm.beacon_rate(
            ap, self._beacon_samples, time.time())
        # Power stays uncoloured — RSSI is too inconsistent across cards/ports to
        # map to a meaningful health gradient without it flickering noise.
        self.query_one("#lbl-pwr", Label).update(
            Text.from_markup(f"Power: [bold]{ap.signal} dBm[/bold]", emoji=False)
        )

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
            # Handshake/PMKID counting + persisted-history back-fill live in
            # focus_model (shared with v2). The "PMKID:" label pads to align its
            # value column with "Handshake: ".
            hs_label.update(Text.from_markup(
                f"Handshake: {fm.handshake_value_markup(ap)}", emoji=False))
            pmkid_label.update(Text.from_markup(
                f"PMKID:     {fm.pmkid_value_markup(ap)}", emoji=False))

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
        blocked = fm.deauth_blocked(ap, self._campaigns())
        self.query_one("#btn-deauth-sel", Button).disabled = (
            self._cursor_mac() is None or blocked)
        self.query_one("#btn-deauth-bcast", Button).disabled = blocked

    def _update_wep_capture(self, ap: AccessPoint) -> None:
        """WEP CAPTURE rows — IVs + a dedicated Replay-status row — plus the
        Crack section (under SECURITY). The usable-IV (crack-sample) count is no
        longer shown here; it lives in SECURITY's Crack line (N/10k usable IVs),
        which is what gates cracking. The derivations are shared with v2."""
        iface = getattr(self.app, "active_interface", None)
        samples = iface.wep_store.crack_sample_count(ap.bssid) if iface else 0
        campaign = self._wep_campaign

        self.query_one("#lbl-ivs", Label).update(Text.from_markup(
            f"IVs: {fm.ivs_value_markup(ap, iface)}", emoji=False))

        replay_markup = (
            "[green]✓ done[/green]" if ap.wep_key is not None
            else fm.replay_status_markup(campaign)
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
        visible, crack_markup, info_markup = fm.crack_section(ap, campaign, samples)
        crack.display = visible
        info.display = visible
        if visible:
            crack.update(Text.from_markup(crack_markup, emoji=False))
            info.update(Text.from_markup(info_markup, emoji=False))

    def _update_fakeauth_line(self) -> None:
        """Render the SECURITY-panel Fake-Auth status from the running campaign."""
        fa_label = self.query_one("#lbl-fakeauth", Label)
        fa_label.display = True
        fa_label.update(Text.from_markup(
            fm.fakeauth_markup(self._wep_campaign, time.time()), emoji=False))

    # ----- Client table ------------------------------------------------------

    def _refresh_clients(self, iface) -> None:
        # focus_model.client_rows owns the filtering (skip our own forged/self
        # STAs); here we just sync the rows into the DataTable, incrementally so
        # the cursor/scroll position is preserved between ticks.
        client_table = self.query_one("#client-table", DataTable)
        for row in fm.client_rows(self.target_ap, iface):
            mac = row.bssid
            if mac not in self._known_clients:
                self._known_clients.add(mac)
                client_table.add_row(
                    Text(mac),
                    Text(f"{row.power} dBm", justify="right"),
                    Text(str(row.packets), justify="right"),
                    key=mac,
                )
            else:
                client_table.update_cell(
                    mac, "signal", Text(f"{row.power} dBm", justify="right")
                )
                client_table.update_cell(
                    mac, "packets", Text(str(row.packets), justify="right")
                )

    # ----- Capture-event log -------------------------------------------------

    def _drain_capture_events(self, ap: AccessPoint, forged_macs: Set[str]) -> None:
        for ev in self._events.poll(ap, forged_macs=forged_macs):
            self._log_capture_event(ev, ap)

    def _log_capture_event(self, ev: CaptureEvent, ap: AccessPoint) -> None:
        if ev.kind == CaptureKind.EAPOL:
            # Per-frame trace: one line per M1-M4 as it lands (incl. M1/M3
            # retransmits), ticking each hashcat-relevant field so the contents
            # are legible as they fly by. Routine, so no solid highlight — the bg
            # chip is reserved for the "Valid 4-Way Handshake" banner below.
            # Completeness is announced once per instance there, never here.
            self._log(eapol_message_markup(ev))
        elif ev.kind == CaptureKind.HANDSHAKE:
            # Significant event → solid highlight. Re-fires per new 4-way. Names
            # the client (last 3 octets) so the user knows which STA completed —
            # and, on a Partial, which one to deauth for another shot.
            essid = escape(ev.ssid or ev.bssid)
            self._log(
                f"[black bold on green] ✓ Valid 4-Way Handshake "
                f"({ev.pair_label}) [/black bold on green] "
                f"[bold]{short_sta(ev.client_mac)}[/bold] for [bold]{essid}[/bold]"
            )
            result = save_handshake(ap, ev.client_mac)
            if result is not None:
                verb = "saved" if result.was_new else "already saved as"
                self._log(f"[dim]({verb} {escape(result.path.name)})[/dim]")
        elif ev.kind == CaptureKind.PMKID:
            # Significant event → same solid highlight as the handshake banner.
            self._log(
                f"[black bold on green] ✓ PMKID captured [/black bold on green] "
                f"from [bold]{short_sta(ev.client_mac)}[/bold]"
            )
            result = save_pmkid(ap, ev.client_mac)
            if result is not None:
                verb = "saved" if result.was_new else "already saved as"
                self._log(f"[dim]({verb} {escape(result.path.name)})[/dim]")
        elif ev.kind == CaptureKind.DECLOAK:
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

    def _campaigns(self) -> fm.Campaigns:
        """Bundle the live campaign handles for the shared derivations."""
        return fm.Campaigns(
            wep=self._wep_campaign, wps=self._wps_campaign,
            wpa3_down=self._wpa3_down_attack, pbc_busy=self._pbc_busy(),
        )

    def _other_long_running_tx(self, exclude: str = "") -> bool:
        """True if any long-running TX activity is running, EXCLUDING the named
        one (one of ``"wep"`` / ``"wpa3down"`` / ``"wps"`` / ``"pbc"``). Used to
        disable buttons for OTHER attacks so the half-duplex radio is never
        shared. Pass the running attack's name to keep its own Stop button live."""
        return fm.other_long_running_tx(self._campaigns(), exclude)

    def _apply_button(self, selector: str, state: fm.ButtonState) -> None:
        """Apply a derived :class:`focus_model.ButtonState` to a Button widget —
        visibility, enablement, label, and variant in one place."""
        btn = self.query_one(selector, Button)
        btn.display = state.visible
        btn.disabled = state.disabled
        btn.label = state.label
        btn.variant = state.variant

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
                    result = save_wps_pbc(ap, outcome.psk)
                    if result is None:
                        self._log(treelog.leaf("[dim](save failed)[/dim]"))
                    else:
                        verb = "saved" if result.was_new else "already saved as"
                        self._log(treelog.leaf(f"[dim]({verb} {escape(result.path.name)})[/dim]"))
                except Exception:
                    self._log(treelog.leaf("[dim](save failed)[/dim]"))
            else:
                self._log(treelog.leaf_warn(
                    f"{outcome.result.value} [dim]({escape(outcome.detail)})[/dim] — "
                    "retrying while the window's open"))
        except Exception as exc:
            self._log(treelog.leaf_fail(f"capture error: {escape(str(exc))}"))

    def _stop_pbc_capture(self) -> None:
        """Cancel any running PBC capture. Re-invade on a later target is gated
        by ap.has_psk (which persists across target switches), so there is no
        per-target dedup state to reset here."""
        if self._pbc_task is not None:
            self._pbc_task.cancel()
            self._pbc_task = None

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
            # Record on the AP so the win-event detector surfaces it in the
            # Scanner log too (PIN + PSK → two lines).
            camp.target.wps_pin = camp.state.found_pin
            camp.target.wps_pin_psk = camp.state.found_psk
            self._log(treelog.branch_ok(
                f"[black bold on cyan]  WPS PIN for {ssid}: "
                f"{escape(camp.state.found_pin)}  [/black bold on cyan]"))
            self._log(treelog.branch(
                f"[black bold on green] Password for {ssid}: "
                f"\"{escape(camp.state.found_psk or '')}\" [/black bold on green]"))
            # Persist the recovered creds so they survive exit + light the
            # Scanner badge on next start. The saved path is its own └─► leaf
            # below the Password branch so the tree closes cleanly.
            try:
                result = save_wps_pin(
                    camp.target, camp.state.found_pin, camp.state.found_psk or "")
                if result is None:
                    self._log(treelog.leaf("[dim](save failed)[/dim]"))
                else:
                    verb = "saved" if result.was_new else "already saved as"
                    self._log(treelog.leaf(f"[dim]({verb} {escape(result.path.name)})[/dim]"))
            except Exception:
                self._log(treelog.leaf("[dim](save failed)[/dim]"))
        else:
            self._log(treelog.leaf(
                f"[yellow]WPS PIN stopped[/yellow] "
                f"[dim]({camp.state.tested} tested, phase {camp.state.phase})[/dim]"))

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
        if bid == "btn-deauth-bcast":
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
        elif bid == "btn-chop":
            self._toggle_chop()

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
            # The harvest attack populates ap.handshakes[<our client>].pmkid;
            # save the artifact and close the tree with the resulting filename.
            result = None
            for client_mac, hs in ap.handshakes.items():
                if hs.pmkid == pmkid:
                    result = save_pmkid(ap, client_mac)
                    break
            if result is None:
                self._log(treelog.leaf("[dim](save failed)[/dim]"))
            else:
                verb = "saved" if result.was_new else "already saved as"
                self._log(treelog.leaf(f"[dim]({verb} {escape(result.path.name)})[/dim]"))
        else:
            self._log(treelog.branch_fail("[bold red]No PMKID harvested[/bold red] — possible reasons:"))
            self._log(treelog.branch("[dim]AP may not advertise a PMKID KDE[/dim]"))
            self._log(treelog.leaf("[dim]PMF / status rejected the request[/dim]"))

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

    def _toggle_chop(self) -> None:
        """Chop button. A sub-mode of a running campaign — byte-by-byte
        ICV-oracle decryption to forge an ARP seed when none can be captured."""
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
            f"{fm.format_duration(duration)}{failed}[/dim]"
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

