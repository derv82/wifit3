"""``FocusViewV2``: the spatial "router-admin" Focus redesign (landscape v1).

Layout (top to bottom):
- **Top bar** (fixed height): an "action area" on the left (the back button +
  the encryption-conditional attack buttons, all the clickables in one place)
  then the status line, expanding to fill and stay centered.
- **Mid band**: card | packet-dashboard | router. Card and router are fixed-width
  (the art is 20 cells) and vertically centered; the dashboard fills the
  middle. The band's height is capped so the sparklines reach full 2-row height
  and the endpoint columns fit, then extra terminal height goes to the bottom.
- **Bottom band**: LOG (fluid width) | CLIENTS (fixed width). Grows once the mid
  band is satisfied, so tall terminals show more log lines + clients.

Power + signal live above the router ESSID (the live rainbow signal bar), not in
the top bar. Portrait is deferred.
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Set

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from wifit3.campaigns import treelog
from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.pmkid import PmkidHarvestAttack
from wifit3.campaigns.wep import WepCampaign
from wifit3.campaigns.eviltwin import EvilTwinCampaign, EvilTwinInput
from wifit3.ui.screens.focus_v2.eviltwin_modal import EvilTwinInputModal
from wifit3.campaigns.pin import WpsCampaign, load_run_state, run_progress_line
from wifit3.campaigns.deauth import DeauthCampaign
from wifit3.campaigns.pbc import WpsPbcCapture
from wifit3.campaigns.wps.registrar import PinResult
from wifit3.crack.handshake import handshake_uncrackable_label
from wifit3.persist.save import (
    save_handshake, save_pmkid, save_wep_key, save_wps_pbc, save_wps_pin,
)
from wifit3.persist.config import Config

from ... import focus_model as fm
from ...capture_events import (
    CAPTURE_TOAST_TITLES, DECLOAK_METHOD_LABELS, CaptureEvent, CaptureEventDetector, CaptureKind,
)
from ...capture_log import short_sta
from ...eapol_aggregate import EapolAggregator
from ... import pmkid_log
from ...encryption_format import wep_key_ascii
from .card_endpoint import CardEndpoint
from .tx_picker import TxDevicePicker
from .clients_list import ClientsList
from .packet_dashboard import PacketDashboard
from .log_band import LogBand
from .router_endpoint import RouterEndpoint
from . import art

if TYPE_CHECKING:
    from wifit3.ui.app import WifiteApp

logger = logging.getLogger(__name__)

_ENDPOINT_W = 20  # the .ans art is exactly 20 cells wide
_TOPBAR_H = 3
_CHROME_H = 2  # Buffer for Textual's Header & Footer (1 row each)
_BORDER = "ansi_cyan"  # Border/title color for  LOG / CLIENTS panels

# Mid-band height
_CENTER_MAX = 13
_CENTER_MIN = 7
_BOTTOM_MIN = 6
# Horizontal padding on the mid row
_PAD_START = 80
_PAD_RATE = 0.4

_PBC_RETRY_COOLDOWN_S = 3.0

_ATTACK_BUTTONS = [
    ("btn-gen-ivs", "ARP Replay"), ("btn-chop", "ChopChop"), ("btn-deauth", "AutoDeauth"),
    ("btn-pmkid", "PMKID"), ("btn-wps-pin", "WPS PIN"), ("btn-eviltwin", "EvilTwin"),
    ("btn-stop-pbc", "Stop PBC"),
]

# Campaign-key -> the button id whose visible/enabled state
_CAMPAIGN_BUTTON_ID = {cls.key: cls.button_id for cls in fm.BUTTON_CAMPAIGNS}
_CAMPAIGN_BUTTON_ID["chop"] = "btn-chop"  # WEP sub-action

# Static button tooltips, keyed by live label so toggled buttons get idle/run-specific tips.
_BUTTON_TIPS = {
    "ARP Replay": "Listen for & replay ARP packets",
    "Stop Replay": "Stop the entire WEP campaign.",
    "ChopChop": "Forge a replayable packet",
    "Stop Chop": "Interrupt chopping and return to ARP replay",
    "PMKID": "Associate to extract PMKID (some APs not applicable)",
    "WPS PIN": "Start a WPS PIN brute-force campaign",
    "EvilTwin": "Punt clients onto a WPA2 twin to capture a crackable handshake",
    "Stop EvilTwin": "Tear down the twin and return clients to the AP",
}


# Pretty-print a capture filename, skipping the <bssid>_<epoch> middle
_FILENAME_MIDDLE = re.compile(r"_[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5}_\d+_")


def _save_line(result) -> str:
    """Short, dim 'saved/exists: captures/<essid>_…_<kind>.<ext>' line for a save result."""
    verb = "saved" if result.was_new else "exists"
    name = result.path.name
    m = _FILENAME_MIDDLE.search(name)
    short = f"{name[:m.start()]}_…_{name[m.end():]}" if m else name
    return f"[dim]{verb}: captures/{escape(short)}[/dim]"


def _wep_key_chip(key_hex) -> str:
    """Black-bold-on-cyan WEP key chip (bare hex for non-printable keys)."""
    if not key_hex:
        return "[dim]?[/dim]"
    return f"[black bold on cyan] {wep_key_ascii(key_hex)} [/black bold on cyan]"


class FocusViewV2(Screen):
    app: "WifiteApp"

    HORIZONTAL_BREAKPOINTS = [(0, "-compact"), (100, "-normal"), (140, "-wide")]

    # Attack hotkeys come from the campaign registry
    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("d", "deauth_all", "Deauth", show=True),
        *[Binding(cls.hotkey[0], f"campaign('{cls.key}')", cls.hotkey[1], show=True)
          for cls in fm.BUTTON_CAMPAIGNS if cls.hotkey],
        Binding("c", "campaign('chop')", "ChopChop", show=True),
        Binding("w", "wps_pbc_mode", "WPS PBC", show=True),
        Binding("s", "silence", "Silence", show=True),
        Binding("s", "unsilence", "unSilence", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

    _DEAUTH_SEL_ROUNDS = 10
    _DEAUTH_BCAST_COUNT = 20

    CSS = """
    FocusViewV2 { layout: vertical; background: $surface; }

    #topbar { height: %(top)d; }
    #actions { width: auto; height: 100%%; }
    #topbar Button { height: 3; width: auto; min-width: 0; margin: 0 1 0 0; }
    /* No background override on .attack-btn */
    #status { width: 1fr; height: 3; content-align: center middle; text-align: center; }
    #rspacer { width: 0; height: 1; }

    #mid { height: 1fr; }
    #card, #router { width: %(ew)d; align: center middle; }
    /* Router art is a row shorter than the card's, so bottom-align it. */
    #router { align: center bottom; }
    #dashboard { width: 1fr; height: 100%%; padding: 0 1; }
    .endpoint-art { width: %(ew)d; background: transparent; }
    .card-static, .ap-static { width: 100%%; height: 1; text-align: center; color: $text-muted; }
    .card-dynamic { width: 100%%; height: 1; text-align: center; color: $accent; }
    .ap-essid { width: 100%%; height: 1; text-align: center; text-style: bold; }
    .ap-power { width: 100%%; height: 1; text-align: center; }

    #bottom { height: 1fr; }
    #log { width: 1fr; height: 100%%; border: round %(border)s;
           border-title-color: %(border)s; border-title-style: bold; padding: 0 1; }
    #log-rich { width: 100%%; height: 1fr; background: transparent; border: none; padding: 0; }
    #clients { width: 40; height: 100%%; border: round %(border)s;
               border-title-color: %(border)s; border-title-style: bold; padding: 0 1; }
    /* Rows scroll inside a fixed-height region; the broadcast button stays pinned. */
    #client-rows { width: 100%%; height: 1fr; }

    .bcast-btn { width: 100%%; height: 1; min-width: 0; border: none; margin: 0 0 1 0;
                 background: $error; color: $text; content-align: center middle; }
    .client-row { height: 1; width: 100%%; }
    .cl-bssid { width: 17; }
    .cl-pwr { width: 5; text-align: right; }
    .cl-pkts { width: 6; text-align: right; }
    .cl-deauth { width: 3; min-width: 3; height: 1; border: none; margin: 0 0 0 1;
                 background: red; color: white; content-align: center middle; }
    """ % {"ew": _ENDPOINT_W, "top": _TOPBAR_H, "border": _BORDER}

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._snap = fm.fake_snapshot()
        self._target_ap = None
        self._last_status: list[str] | None = None   # last-pushed headline; skip no-op repaints
        self._beacon_samples: deque = deque()
        self._events = CaptureEventDetector(granular_eapol=True)
        self._eapol_agg = EapolAggregator(settle_s=3.0)
        self._tick_timer = None
        # Live campaign handles
        self._wep_campaign: Optional[WepCampaign] = None
        self._wps_campaign: Optional[WpsCampaign] = None
        self._eviltwin_attack: Optional[EvilTwinCampaign] = None
        self._pbc_campaign: Optional[WpsPbcCapture] = None
        self._pbc_user_stopped = False
        self._pbc_retry_after = 0.0   # monotonic time before which we won't re-arm a PBC retry
        self._pmkid_campaign: Optional[PmkidHarvestAttack] = None
        self._deauth_campaign: Optional[DeauthCampaign] = None
        self._prev_stats = None
        self._campaign_toggles = {
            "wep": self._toggle_generate_ivs, "pmkid": self._toggle_pmkid,
            "wps": self._toggle_wps_pin, "chop": self._toggle_chop,
            "deauth": self._toggle_deauth,
        }
        self._binding_sig: Optional[tuple] = None
        self._rspacer_w = -1                          # last-set spacer width; skip no-op relayouts

    # ----- compose -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        self._snap = self._snapshot()
        yield Header()
        with Horizontal(id="topbar"):
            with Horizontal(id="actions"):
                yield Button("‹ Scanner", id="back")
                # The full attack set is composed once (hidden); derive_buttons shows the ones that fit the target.
                for bid, label in _ATTACK_BUTTONS:
                    btn = Button(label, id=bid, classes="attack-btn")
                    btn.display = False
                    yield btn
            yield Static(self._render_status(self._snap.status), id="status")
            # Right spacer to accurately align status to sparklines.
            yield Static("", id="rspacer")
        with Horizontal(id="mid"):
            yield CardEndpoint(self._snap, id="card")
            yield PacketDashboard(self._snap.dashboard, id="dashboard")
            yield RouterEndpoint(self._snap, id="router")
        with Horizontal(id="bottom"):
            yield LogBand(self._snap.log_lines, id="log")
            yield ClientsList(self._snap.clients, id="clients")
        yield Footer()

    async def on_mount(self) -> None:
        self._tick_timer = self.set_interval(1 / 10, self._tick)
        self._distribute()
        await self._enter_target()

    async def on_screen_resume(self) -> None:
        # Full re-acquire only on a target change; a same-target return keeps the live view.
        target = getattr(self.app, "target_ap", None)
        if target is not self._target_ap:
            await self._enter_target()
        elif target is not None:
            # Re-pin the pool to the target's channel on re-entry (STACK across channel-capable cards).
            array = self.app.array
            if array is not None:
                ok = await array.set_channel(target.channel, scan=False)
                logger.info("[FOCUS] re-pin: bssid=%s ch=%s -> %s",
                            target.bssid, target.channel, ok)

    def on_resize(self) -> None:
        self._distribute()

    # ----- snapshot building -------------------------------------------------

    def _pbc_busy(self) -> bool:
        return self._pbc_campaign is not None and not self._pbc_campaign.done

    def _campaigns(self) -> fm.Campaigns:
        return fm.Campaigns(
            wep=self._wep_campaign, wps=self._wps_campaign,
            deauth=self._deauth_campaign, eviltwin=self._eviltwin_attack,
            pbc_busy=self._pbc_busy(),
        )

    def _snapshot(self) -> fm.FocusSnapshot:
        ap = getattr(self.app, "target_ap", None)
        if ap is None:
            return fm.fake_snapshot()
        array = self.app.array
        return fm.build_snapshot(
            ap, array, self._campaigns(), self._beacon_samples, time.time())

    @staticmethod
    def _render_status(status) -> Text:
        return Text("\n").join(Text.from_markup(s, emoji=False) for s in status)

    def _apply_button(self, selector: str, state: fm.ButtonState) -> None:
        btn = self.query_one(selector, Button)
        btn.display = state.visible
        btn.disabled = state.disabled
        btn.label = state.label
        btn.variant = state.variant
        btn.tooltip = state.reason or _BUTTON_TIPS.get(str(state.label))

    def _sync_card(self) -> None:
        """Refresh the card endpoint (picker + art) from the live pool, polled because WlanArray has
        no arrival callback. The shown card is the TX card: the campaign's locked one, else
        select_iface's pick for this target."""
        array = self.app.array
        members = array.members if array else []
        active = Campaign.active
        ap = getattr(self.app, "target_ap", None)
        if active is not None:
            primary = active.iface
        elif ap is not None and array is not None:
            primary = array.select_iface(ap.channel)
        else:
            primary = None
        primary = primary or art.pick_primary(members)   # no capable card: still show something
        card = self.query_one("#card", CardEndpoint)
        card.set_art(art.art_path_for(primary) if primary is not None else art.pool_art(members))
        card.sync_picker(members, ap.channel if ap is not None else None,
                         primary, active is not None)
        card.update_bssid(members[0].mac_address if len(members) == 1 else None)

    def on_tx_device_picker_selected(self, event: TxDevicePicker.Selected) -> None:
        """User pinned a TX card in the picker: record the preference and sync the endpoint."""
        if self.app.array is not None:
            self.app.array.prefer(event.iface)
        self._sync_card()

    def _refresh_buttons(self) -> None:
        """Drive the conditional attack buttons from derive_buttons."""
        ap = getattr(self.app, "target_ap", None)
        if ap is None:
            return
        for bid, state in fm.derive_buttons(ap).items():
            self._apply_button(f"#{bid}", state)
        stop_pbc = self.query_one("#btn-stop-pbc", Button)
        if self._pbc_busy():
            stopping = getattr(self._pbc_campaign, "stopped", False)
            stop_pbc.display = True
            stop_pbc.disabled = stopping         # already draining → no double-stop
            stop_pbc.variant = "error"
            stop_pbc.label = "Stopping…" if stopping else "Stop PBC"
        else:
            stop_pbc.display = False

    # ----- target (re)acquisition --------------------------------------------

    async def _enter_target(self) -> None:
        """Bind to ``app.target_ap``: stop campaigns, reset state, update panels/radio/log."""
        self._stop_eviltwin()
        self._stop_generate_ivs()
        self._stop_pbc_capture()
        self._stop_wps_pin()
        self._stop_pmkid()

        ap = getattr(self.app, "target_ap", None)
        self._target_ap = ap
        if ap is None:
            return
        array = self.app.array
        logger.info("[FOCUS] enter: ssid=%r bssid=%s ch=%s", ap.ssid, ap.bssid, ap.channel)

        self._beacon_samples.clear()
        self._events.reset()
        self._eapol_agg.reset()
        self._prev_stats = None        # drop the old target's counters
        self.query_one("#log", LogBand).clear()

        snap = self._snapshot()
        self._snap = snap
        self.query_one("#dashboard", PacketDashboard).reconfigure(snap.dashboard, array, ap.bssid)
        self.query_one("#card", CardEndpoint).update_dynamic(snap)
        self._sync_card()
        self.query_one("#router", RouterEndpoint).update_dynamic(snap)
        self.query_one("#status", Static).update(self._render_status(snap.status))
        self.query_one("#clients", ClientsList).sync(snap.clients)
        self._refresh_buttons()
        self._balance_status()
        self._refresh_status_footer()  # dashboard footer (cleared by reconfigure)

        # Log initial "target acquired"
        enc = fm.encryption_chip(ap)
        if ap.ssid:
            chip = f"[black bold on cyan] {escape(ap.ssid)} [/black bold on cyan]"
            self._log(f"[bold]Target acquired:[/bold] {chip}")
        else:
            self._log("[bold]Target acquired:[/bold] "
                      "[dim italic]cloaked network (hidden SSID)[/dim italic]")
        self._log(treelog.branch(f"[dim]Encryption:[/dim] {enc}"))
        self._log(treelog.branch(f"[dim]BSSID:[/dim] {ap.bssid}"))
        if array:
            try:
                ok = await array.set_channel(ap.channel, scan=False)
            except Exception:
                logger.exception("Focus v2 channel tune failed")
                ok = False
            if ok:
                self._log(treelog.leaf(f"Tuned to [cyan]channel {ap.channel}[/cyan]"))
            else:
                self._log(treelog.leaf(
                    f"[yellow]Tried to tune to channel {ap.channel}[/yellow]"))
        else:
            self._log(treelog.leaf("[yellow]no interface: passive view only[/yellow]"))

        if ap.pmf_required:
            self._log("[bold yellow]PMF Required:[/] "
                      "AP requires [bold]Protected Management Frames[/]")
            self._log(treelog.leaf("[italic]Deauth[/] attacks have been disabled"))

        self._log_persisted_history(ap)

        if Config.is_silenced(ap.bssid):
            self._log_silenced()
            return

        enc = (ap.encryption or "").upper()
        if enc == "WEP":
            self._log("[bold italic]Passively listening[/bold italic] for [bold]WEP IVs[/bold]")
        elif enc not in ("OPEN", "", "WPA3 "):
            # 'Crackable' because the pipeline filters SAE-only out.
            self._log("[bold italic]Passively listening[/bold italic] for")
            self._log(treelog.branch("Crackable 4-Way [bold]Handshakes[/bold]"))
            self._log(treelog.leaf("Crackable [bold]PMKIDs[/bold]"))

    def _log_persisted_history(self, ap) -> None:
        """On focus init, print captures/ artifacts for this AP to the log."""
        wps_state = load_run_state("captures", ap.bssid)
        wps_progress = run_progress_line(wps_state) if wps_state else None
        if not ap.persisted and not wps_progress:
            return
        by_type: dict[str, list] = {}
        for cap in sorted(ap.persisted, key=lambda c: c.timestamp, reverse=True):
            by_type.setdefault(cap.type, []).append(cap)

        nouns = {"HS": "Handshake", "PMKID": "PMKID", "WEP": "WEP Key", "WPS": "WPS PSK"}
        # Newest of each kind, newest kind first; the label column is padded so the dates line up.
        rows = sorted(((k, caps[0], len(caps)) for k, caps in by_type.items()),
                      key=lambda r: r[1].timestamp, reverse=True)
        if rows:
            self._log("[bold]Existing captures[/bold] in [cyan]captures/[/cyan]:")
        label_w = max((len(f"{nouns[k]} ({n})") for k, _cap, n in rows), default=0)
        for i, (kind, cap, n) in enumerate(rows):
            # The WPS progress leaf, if present, takes the └, so a saved row is the last leaf only when nothing follows.
            last = i == len(rows) - 1 and wps_progress is None
            line = treelog.leaf if last else treelog.branch
            pad = " " * (label_w - len(f"{nouns[kind]} ({n})"))
            label = f"[bold cyan]{nouns[kind]}[/bold cyan] [dim]({n})[/dim]{pad}"
            dt = datetime.fromtimestamp(cap.timestamp)
            if kind == "WEP":
                self._log(line(f"{label}  {_wep_key_chip(cap.value)} "
                               f"[dim]{dt:%Y-%m-%d %H:%M}[/dim]"))
            elif kind == "WPS":
                self._log(line(f"{label}  [black bold on cyan] {escape(cap.value or '?')} "
                               f"[/black bold on cyan] [dim]{dt:%Y-%m-%d %H:%M}[/dim]"))
            else:
                self._log(line(f"{label}  {dt:%Y-%m-%d} "
                               f"[dim]{dt:%H:%M}[/dim]"))
        if wps_progress is not None:
            self._log(treelog.leaf(wps_progress) if rows else wps_progress)

    # ----- per-tick paint ----------------------------------------------------

    def _tick(self) -> None:
        if self._target_ap is None or not self.is_current:
            return
        ap = self._target_ap

        # Campaign lifecycle teardowns once complete.
        if self._wps_campaign is not None and (
                self._wps_campaign.state.phase == "done"
                or self._wps_campaign.status in ("failed", "error")):
            self._stop_wps_pin()
        if self._wep_campaign is not None and self._wep_campaign.recovered_key is not None:
            result = save_wep_key(ap, self._wep_campaign.recovered_key)
            if result is not None:
                self._log(treelog.leaf(_save_line(result)))
            self._stop_generate_ivs()
        if self._pmkid_campaign is not None and self._pmkid_campaign.done:
            self._finish_pmkid()
        if self._deauth_campaign is not None and self._deauth_campaign.done:
            self._finish_deauth()
        if self._eviltwin_attack is not None and self._eviltwin_attack.done:
            self._finish_eviltwin()
        if self._pbc_campaign is not None and self._pbc_campaign.done:
            self._finish_pbc_capture(ap)
        # Clear the manual-stop suppression when the window closes; a fresh one re-arms.
        if not ap.wps_pbc_active:
            self._pbc_user_stopped = False
        if self._should_auto_invade_pbc(ap):
            self._start_pbc_capture(ap)

        snap = self._snapshot()
        self._snap = snap
        if snap.status != self._last_status:
            self._last_status = snap.status
            self.query_one("#status", Static).update(self._render_status(snap.status))
        self.query_one("#card", CardEndpoint).update_dynamic(snap)
        self._sync_card()
        self.query_one("#router", RouterEndpoint).update_dynamic(snap)
        clients = self.query_one("#clients", ClientsList)
        clients.sync(snap.clients)
        clients.set_deauth_enabled(not fm.deauth_blocked(ap))
        self._refresh_buttons()
        self._balance_status()
        self._sync_bindings()
        self._refresh_status_footer()
        array = self.app.array
        self._drive_leds(ap, array)
        self._drain_capture_events(ap, array.forged_macs if array else set(), time.time())

    def _should_auto_invade_pbc(self, ap) -> bool:
        if not (ap.wps_pbc_active and self.app.pbc_enabled):
            return False
        if Config.is_silenced(ap.bssid) or ap.is_hidden:
            return False
        if ap.has_psk or self._pbc_user_stopped:
            return False
        if time.monotonic() < self._pbc_retry_after or self._pbc_busy():
            return False
        # no other attack owns the target
        return (self._wep_campaign is None and self._wps_campaign is None
                and self._deauth_campaign is None and self._eviltwin_attack is None)

    def _distribute(self) -> None:
        """Fill the mid band to full 2-row sparklines."""
        avail = max(1, self.size.height - _TOPBAR_H - _CHROME_H)
        center = min(_CENTER_MAX, max(_CENTER_MIN, avail - _BOTTOM_MIN))
        center = max(1, min(center, avail - 1))
        mid = self.query_one("#mid")
        mid.styles.height = center
        self.query_one("#bottom").styles.height = avail - center
        pad = max(0, round((self.size.width - _PAD_START) * _PAD_RATE))
        mid.styles.padding = (0, pad, 0, pad)
        self._balance_status()

    def _balance_status(self) -> None:
        """Center the status label over the sparklines despite the button row on its left."""
        topbar_w = self.query_one("#topbar").content_size.width
        actions_w = self.query_one("#actions").outer_size.width
        widest = max((Text.from_markup(s, emoji=False).cell_len
                      for s in self._snap.status), default=0)
        spacer = min(actions_w, max(0, topbar_w - actions_w - widest))
        if spacer != self._rspacer_w:
            self._rspacer_w = spacer
            self.query_one("#rspacer", Static).styles.width = spacer

    def _refresh_status_footer(self) -> None:
        """Refresh status footer under sparklines."""
        ap = self._target_ap
        if ap is None:
            return
        array = self.app.array
        lines = [Text.from_markup(m, emoji=False)
                 for m in fm.status_footer_lines(ap, array, self._wep_campaign, time.time())]
        self.query_one("#dashboard", PacketDashboard).set_footer(lines)

    # ----- endpoint LED flicker (instrumentation) ----------------------------

    # Router LED = RX from the target; card LED = TX we send.
    _RX_KEYS = ("beacon", "data", "eapol", "wep_iv")
    _TX_KEYS = ("inject", "deauth")

    def _drive_leds(self, ap, array) -> None:
        """Flicker the endpoint LEDs on real traffic."""
        if array is None:
            return
        snap = array.packet_stats.snapshot(ap.bssid)
        prev, self._prev_stats = self._prev_stats, snap
        if prev is None:   # first frame after (re)acquire, no delta yet
            return
        if any(snap.get(k, 0) > prev.get(k, 0) for k in self._RX_KEYS):
            self.query_one("#router", RouterEndpoint).flicker()
        if any(snap.get(k, 0) > prev.get(k, 0) for k in self._TX_KEYS):
            self.query_one("#card", CardEndpoint).flicker()

    # ----- event log (capture pipeline) --------------------------------------

    def _drain_capture_events(self, ap, forged_macs: Set[str], now: float) -> None:
        # EAPOL + handshake completions go through the aggregator (one tree per client); PMKID / decloak are immediate.
        if Config.is_silenced(ap.bssid):
            return
        for ev in self._events.poll(ap, forged_macs=forged_macs):
            if ev.kind == CaptureKind.EAPOL:
                self._eapol_agg.on_eapol(ev, now)
            elif ev.kind == CaptureKind.HANDSHAKE:
                # Save instantly
                result = save_handshake(ap, ev.client_mac)
                hint = _save_line(result) if result is not None else None
                self._emit_lines(self._eapol_agg.on_handshake(ev, now, save_hint=hint))
            else:
                self._log_capture_event(ev, ap)
        # Flush any per-client bursts that have gone quiet; label real-but-uncrackable ones.
        for lines in self._eapol_agg.tick(now, label_for=lambda mac: self._uncrackable_reason(ap, mac)):
            self._emit_lines(lines)

    def _emit_lines(self, lines) -> None:
        for ln in lines:
            self._log(ln)

    def _uncrackable_reason(self, ap, mac: str) -> str | None:
        """The uncrackable-AKM badge (SAE/FT/EAP/OWE) for a client's handshake, else None."""
        hs = ap.handshakes.get(mac)
        return handshake_uncrackable_label(hs) if hs is not None else None

    def _log_capture_event(self, ev: CaptureEvent, ap) -> None:
        if ev.kind == CaptureKind.PMKID:
            self._log(
                f"[black bold on green] ✓ PMKID captured [/black bold on green] "
                f"from [bold]{short_sta(ev.client_mac)}[/bold]"
            )
            result = save_pmkid(ap, ev.client_mac)
            if result is not None:
                self._log(treelog.leaf(_save_line(result)))
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
            log = self.query_one("#log", LogBand)
        except Exception:
            return
        log.write(Text.from_markup(f"[dim]{ts}[/dim]  {markup}", emoji=False))

    # ----- button dispatch ---------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "back":
            await self.action_go_back()
        elif bid == "deauth-all":
            self.run_worker(self._run_deauth_broadcast(), exclusive=True)
        elif bid == "btn-deauth":                       # before the inline-client ✕ (also ends "-deauth")
            self._toggle_deauth()
        elif bid.endswith("-deauth"):
            mac = self.query_one("#clients", ClientsList).client_mac(bid)
            if mac:
                self.run_worker(self._run_deauth_selected(mac), exclusive=True)
        elif bid == "btn-pmkid":
            self._toggle_pmkid()
        elif bid == "btn-wps-pin":
            self._toggle_wps_pin()
        elif bid == "btn-eviltwin":
            self._toggle_eviltwin()
        elif bid == "btn-gen-ivs":
            self._toggle_generate_ivs()
        elif bid == "btn-chop":
            self._toggle_chop()
        elif bid == "btn-stop-pbc":
            self._user_stop_pbc()

    # ----- command-bar (footer hotkeys) --------------------------------------

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        """Drive the footer keys off the same state as the buttons."""
        ap = self._target_ap
        if action == "campaign":
            if ap is None:
                return False
            st = fm.derive_buttons(ap).get(_CAMPAIGN_BUTTON_ID.get(parameters[0]))
            if st is None or not st.visible:
                return False
            return None if st.disabled else True
        if action == "deauth_all":
            if ap is None:
                return False
            return None if fm.deauth_blocked(ap) else True
        if action == "silence":
            return False if (ap is not None and Config.is_silenced(ap.bssid)) else True
        if action == "unsilence":
            return True if (ap is not None and Config.is_silenced(ap.bssid)) else False
        return True

    def _sync_bindings(self) -> None:
        """Repaint the footer only when a key's shown/greyed state changes."""
        ap = self._target_ap
        if ap is None:
            sig: Optional[tuple] = None
        else:
            btns = fm.derive_buttons(ap)
            sig = (tuple((bid, s.visible, s.disabled) for bid, s in btns.items()),
                   fm.deauth_blocked(ap), Config.is_silenced(ap.bssid))
        if sig != self._binding_sig:
            self._binding_sig = sig
            self.refresh_bindings()

    def action_campaign(self, camp_key: str) -> None:
        """Toggle a hotkey's campaign."""
        toggle = self._campaign_toggles.get(camp_key)
        if toggle is not None:
            toggle()
        self._sync_bindings()

    def action_deauth_all(self) -> None:
        """'d': one-shot broadcast deauth, matching the client-panel button."""
        self.run_worker(self._run_deauth_broadcast(), exclusive=True)

    def action_wps_pbc_mode(self) -> None:
        """'w': toggle the shared WPS PBC auto-invade."""
        self.app.pbc_enabled = not getattr(self.app, "pbc_enabled", True)
        self._log_pbc_status()

    def _log_pbc_status(self) -> None:
        if getattr(self.app, "pbc_enabled", True):
            self._log("[bold]WPS PushButton Extraction[/bold] "
                      "[bold green]enabled[/bold green] [dim](press w to toggle)[/dim]")
        else:
            self._log("[bold]WPS PushButton Extraction[/bold] "
                      "[yellow]disabled[/yellow] [dim](detect only, press w to toggle)[/dim]")

    def action_silence(self) -> None:
        self._toggle_silence()

    def action_unsilence(self) -> None:
        self._toggle_silence()

    def _toggle_silence(self) -> None:
        """Flip the focused AP's silenced state (campaigns off, handshakes/PMKIDs ignored)."""
        if self._target_ap is None:
            return
        bssid = self._target_ap.bssid.lower()
        if bssid in Config.silenced_bssids:
            Config.silenced_bssids.remove(bssid)
            self._log("[green bold] ● AP [italic]Un[/italic]Silenced ✓[/]")
            self._log("[dim green]" + treelog.leaf("campaigns enabled, listening for handshakes") + "[/]")
        else:
            Config.silenced_bssids.append(bssid)
            self._log_silenced()
        self.app.persist_config()
        self._refresh_buttons()
        self._sync_bindings()

    def _log_silenced(self) -> None:
        self._log("[yellow bold] ● AP Silenced[/] [red]✗S[/]")
        self._log("[yellow dim]" + treelog.branch("campaigns disabled, handshakes ignored.") + "[/]")
        self._log("[yellow dim]" + treelog.leaf("press [bold]s[/bold] to unsilence.") + "[/]")

    # ----- deauth ------------------------------------------------------------

    async def _run_deauth_broadcast(self) -> None:
        """Worker: broadcast-deauth every station associated with the focused AP."""
        ap = self._target_ap
        array = self.app.array
        card = array.select_iface(ap.channel) if (ap and array) else None
        if not ap or card is None:
            self._log("[red]✗ No target / no card on this channel. Aborting Broadcast.[/red]")
            return
        self._log("[bold]Broadcast de-auth: all clients[/bold]")
        try:
            sent = await card.deauth_broadcast(ap.bssid, count=self._DEAUTH_BCAST_COUNT)
        except Exception as exc:
            logger.exception("Broadcast deauth crashed")
            self._log(treelog.leaf_fail(f"Broadcast failed: {escape(str(exc))}"))
            return
        # Broadcast frames are never ACKed: a neutral leaf (no green ✓).
        self._log(treelog.leaf(f"sent {sent} de-auth frames [dim](AP→broadcast)[/dim]"))

    async def _run_deauth_selected(self, mac: str) -> None:
        """Worker: deauth a specific client (the inline ✕ that was clicked)."""
        ap = self._target_ap
        array = self.app.array
        card = array.select_iface(ap.channel) if (ap and array) else None
        if not ap or card is None:
            self._log("[red]✗ No target / no card on this channel. Aborting Deauth.[/red]")
            return
        self._log(f"[bold]De-authenticating Client {escape(mac)}[/bold]")
        try:
            res = await card.deauth_client(ap.bssid, mac, rounds=self._DEAUTH_SEL_ROUNDS)
        except Exception as exc:
            logger.exception("Deauth %s crashed", mac)
            self._log(treelog.leaf_fail(f"Deauth failed: {escape(str(exc))}"))
            return
        self._log(treelog.branch(
            f"sent {res.total_sent} de-auth frames "
            f"[dim](AP↔Client ×{res.client_sent})[/dim]"))
        if not res.measured:  # card lacks TX-ACK detection, nothing to confirm
            self._log(treelog.leaf("[dim]delivery not measured (no TX-ACK on this card)[/dim]"))
            return
        detail = f"[dim](client {res.client_acks}/{res.client_sent} · AP {res.ap_acks}/{res.ap_sent})[/dim]"
        if res.total_acked:
            self._log(treelog.leaf(
                f"[bold][cyan]{res.total_acked}[/cyan]/{res.total_sent} de-auths ACK'd[/bold] {detail}"))
        else:
            self._log(treelog.leaf(
                f"[bold][red]0[/red]/{res.total_sent} de-auths ACK'd[/bold] [dim](silent AP & client)[/dim]"))

    # ----- PMKID -------------------------------------------------------------

    def _toggle_pmkid(self) -> None:
        if self._pmkid_campaign is not None:
            self._user_stop_pmkid()
        else:
            self._start_pmkid()
        self._refresh_buttons()

    def _start_pmkid(self) -> None:
        if self._pmkid_campaign is not None:  # already harvesting (or finishing), ignore
            return
        ap = self._target_ap
        array = self.app.array
        if not ap or not array:
            self._log("[red]✗ No target / interface. Aborting PMKID harvest.[/red]")
            return
        self._log(pmkid_log.header(escape(ap.ssid or ap.bssid)))
        self._pmkid_campaign = PmkidHarvestAttack(
            array, ap, log=lambda m: self._log(treelog.branch(m)))
        self._pmkid_campaign.run()

    def _finish_pmkid(self) -> None:
        """Handle a completed harvest."""
        camp = self._pmkid_campaign
        self._pmkid_campaign = None
        if camp is None:
            return
        if camp.pmkid:
            result = save_pmkid(camp.target, camp.client_mac)
            hint = _save_line(result) if result is not None else None
            self._emit_lines(pmkid_log.verdict_success(hint))
            # Detector skips forged MACs, so toast the active-harvest win here too.
            name = camp.target.ssid or camp.target.bssid
            body = (f"[bold]{escape(name)}[/bold] on channel [bold]{camp.target.channel}[/bold] "
                    f"[dim bold](BSSID: {escape(camp.target.bssid)})[/dim bold]")
            self.notify(body, title=f"{CAPTURE_TOAST_TITLES[CaptureKind.PMKID]} (M1)", timeout=6)
        elif getattr(camp, "stopped", False):
            self._log(treelog.leaf_fail("[bright_red bold]Stopped harvest[/]"))
        else:
            self._emit_lines(pmkid_log.verdict_failure(camp.fail_reason))

    def _stop_pmkid(self) -> None:
        if self._pmkid_campaign is not None:
            self._pmkid_campaign.request_stop()
            self._pmkid_campaign = None

    def _user_stop_pmkid(self) -> None:
        """The 'Stop PMKID' button."""
        if self._pmkid_campaign is not None:
            self._pmkid_campaign.request_stop()

    # ----- Deauth ------------------------------------------------------------

    def _toggle_deauth(self) -> None:
        if self._deauth_campaign is not None:
            self._user_stop_deauth()
        else:
            self._start_deauth()
        self._refresh_buttons()

    def _start_deauth(self) -> None:
        if self._deauth_campaign is not None:
            return
        ap = self._target_ap
        array = self.app.array
        if not ap or not array:
            self._log("[red]✗ No target / interface. Aborting Deauth.[/red]")
            return
        self._log(f"[bold]Deauth[/bold] of [bold]{escape(ap.ssid or ap.bssid)}[/bold]: "
                  "forcing a re-handshake")
        self._deauth_campaign = DeauthCampaign(array, ap, log=self._log)
        self._deauth_campaign.run()

    def _user_stop_deauth(self) -> None:
        """The 'Stop Deauth' button."""
        if self._deauth_campaign is not None:
            self._deauth_campaign.request_stop()

    def _finish_deauth(self) -> None:
        """Reap a completed deauth run. A captured handshake is saved+toasted by the
        always-on capture path (CaptureKind.HANDSHAKE); we only log the campaign's end."""
        camp = self._deauth_campaign
        self._deauth_campaign = None
        if camp is None:
            return
        if camp.captured:
            self._log("[bold green]✓ Deauth provoked a crackable handshake[/bold green]")
        else:
            self._log("[bright_red bold]Deauth stopped[/]")

    # ----- EvilTwin ----------------------------------------------------------

    def _toggle_eviltwin(self) -> None:
        if self._eviltwin_attack:
            self._stop_eviltwin()
        else:
            self._start_eviltwin()
        self._refresh_buttons()

    def _start_eviltwin(self) -> None:
        ap = self._target_ap
        array = self.app.array
        if not ap or not array or not array.members:
            self._log("[red]✗ No target / interface. Cannot start EvilTwin.[/red]")
            return
        self.app.push_screen(EvilTwinInputModal(ap, array.members), self._on_eviltwin_input)

    def _on_eviltwin_input(self, evil_input: Optional[EvilTwinInput]) -> None:
        if evil_input is None:
            return
        ap, array = self._target_ap, self.app.array
        if not ap or not array:
            return
        try:
            self._eviltwin_attack = EvilTwinCampaign(array, ap, evil_input)
            self._eviltwin_attack.run()
        except Exception as exc:
            logger.exception("EvilTwin start failed")
            self._log(f"[bold red]✗ EvilTwin failed to start:[/bold red] {escape(str(exc))}")
            self._eviltwin_attack = None
            return
        self._log(f"[bold cyan]EvilTwin[/bold cyan] of [bold cyan]"
                  f"{escape(ap.ssid or ap.bssid)}[/bold cyan] active on ch {evil_input.twin_channel}"
                  f" [dim]({evil_input.twin_bssid})[/dim]")
        self._log(treelog.branch(f"[italic]punting clients[/italic] [dim]on[/dim] ch {ap.channel}"))
        self._log(treelog.leaf("[dim]waiting for clients to auth…[/dim]"))

    def _stop_eviltwin(self) -> None:
        """User-initiated stop. Auto-stop on capture is reaped by `_finish_eviltwin`."""
        if not self._eviltwin_attack:
            return
        self._eviltwin_attack.request_stop()
        self._eviltwin_attack = None
        self._log("[bold red]EvilTwin stopped[/bold red]")

    def _finish_eviltwin(self) -> None:
        """Reap a campaign that ran to completion on its own (its task is done, the radio is
        released). Captured is the normal path (the save banner already fired); a done-without-capture
        is a crash the base logged."""
        captured = self._eviltwin_attack.captured
        self._eviltwin_attack = None
        if captured:
            self._log("[bold green]✓ EvilTwin captured a crackable handshake[/bold green]")
        else:
            self._log("[bold red]EvilTwin stopped[/bold red]")

    # ----- WEP: Generate IVs (Replay) + Chop ---------------------------------

    def _toggle_generate_ivs(self) -> None:
        camp = self._wep_campaign
        if camp is not None and camp.recovered_key is None:
            self._stop_generate_ivs()
        else:
            if camp is not None:  # a finished campaign still around, clear it
                self._stop_generate_ivs()
            self._start_generate_ivs()
        self._refresh_buttons()

    def _start_generate_ivs(self) -> None:
        ap = self._target_ap
        array = self.app.array
        if not ap or not array:
            self._log("[red]✗ No target / interface. Cannot Generate IVs.[/red]")
            return
        try:
            self._wep_campaign = WepCampaign(array, ap, log_callback=self._log)
            self._wep_campaign.run()
        except Exception as exc:
            logger.exception("Generate IVs start failed")
            self._log(f"[bold red]✗ Generate IVs failed to start:[/bold red] {escape(str(exc))}")
            self._wep_campaign = None

    def _stop_generate_ivs(self) -> None:
        if not self._wep_campaign:
            return
        self._wep_campaign.request_stop()
        self._wep_campaign = None

    def _toggle_chop(self) -> None:
        camp = self._wep_campaign
        if camp is None:
            self._log("[yellow]Start Replay first[/yellow] [dim](ChopChop "
                      "manufactures an ARP seed for the replay engine)[/dim]")
            return
        if camp.chop_active:
            camp.stop_chop()
            self._log("[cyan]→ Chop stopped[/cyan] [dim](back to ARP replay)[/dim]")
        else:
            camp.start_chop()
        self._refresh_buttons()

    # ----- WPS PIN -----------------------------------------------------------

    def _toggle_wps_pin(self) -> None:
        if self._wps_campaign is None:
            self._start_wps_pin()
        else:
            self._stop_wps_pin()
        self._refresh_buttons()

    def _start_wps_pin(self) -> None:
        ap = self._target_ap
        array = self.app.array
        if not ap or not array:
            self._log("[red]✗ No target / pool. Cannot start WPS PIN.[/red]")
            return
        # Warn when the elected card can't HW-ACK a spoofed MAC (still runs PIN fine, just spammy).
        card = array.select_iface(ap.channel)
        if card is not None:
            warning = card.active_monitor_warning()
            if isinstance(warning, str):
                self._log(warning)
        self._launch_wps_pin(ap, array)

    def _launch_wps_pin(self, ap, array) -> None:
        try:
            name = escape(ap.ssid or ap.bssid)
            self._log(f"[bold]WPS PIN brute[/bold] started on [bold cyan]{name}[/bold cyan]")
            self._wps_campaign = WpsCampaign(
                array, ap, log=lambda m: self._log(treelog.branch(m)))
            self._wps_campaign.run()
        except Exception as exc:
            logger.exception("WPS PIN start failed")
            self._log(f"[bold red]✗ WPS PIN failed to start:[/bold red] {escape(str(exc))}")
            self._wps_campaign = None

    def _stop_wps_pin(self) -> None:
        if self._wps_campaign is None:
            return
        camp = self._wps_campaign
        camp.request_stop()
        self._wps_campaign = None
        ssid = escape(camp.target.ssid or camp.bssid)
        if camp.state.found_pin:
            camp.target.wps_pin = camp.state.found_pin
            camp.target.wps_pin_psk = camp.state.found_psk
            self._log(treelog.branch_ok(
                f"[black bold on cyan]  WPS PIN for {ssid}: "
                f"{escape(camp.state.found_pin)}  [/black bold on cyan]"))
            self._log(treelog.branch(
                f"[black bold on green] Password for {ssid}: "
                f"\"{escape(camp.state.found_psk or '')}\" [/black bold on green]"))
            try:
                result = save_wps_pin(
                    camp.target, camp.state.found_pin, camp.state.found_psk or "")
                if result is None:
                    self._log(treelog.leaf("[dim](save failed)[/dim]"))
                else:
                    self._log(treelog.leaf(_save_line(result)))
            except Exception:
                self._log(treelog.leaf("[dim](save failed)[/dim]"))
        elif getattr(camp, "fail_reason", None):
            self._log(treelog.leaf_fail(
                f"[bold red]giving up:[/bold red] [yellow]{escape(camp.fail_reason)}[/yellow]"))
        else:
            self._log(treelog.leaf(
                f"[yellow]WPS PIN stopped[/yellow] "
                f"[dim]({camp.state.tested} tested, phase {camp.state.phase})[/dim]"))

    # ----- WPS PBC auto-capture ----------------------------------------------

    def _start_pbc_capture(self, ap) -> None:
        array = self.app.array
        if not array:
            return
        self._log("[bold cyan]WPS PushButton:[/bold cyan] [bold green]Window Open[/bold green] "
                  "(auto-capturing PSK)")
        self._pbc_campaign = WpsPbcCapture(
            array, ap, log=lambda m: self._log(treelog.branch(m)))
        self._pbc_campaign.run()

    def _finish_pbc_capture(self, ap) -> None:
        """Handle a completed PBC attempt."""
        camp = self._pbc_campaign
        self._pbc_campaign = None
        if camp is None:
            return
        if getattr(camp, "stopped", False):
            self._log(treelog.leaf(
                "[yellow]stopped[/yellow] [dim](radio freed; auto-invade resumes on "
                "the next window)[/dim]"))
            return
        if camp.error is not None:
            self._log(treelog.leaf_fail(f"capture error: {escape(str(camp.error))}"))
            return
        outcome = camp.outcome
        if outcome is None:
            return
        if outcome.result is PinResult.SUCCESS:
            ap.wps_pbc_psk = outcome.psk
            name = escape(outcome.ssid or ap.ssid or ap.bssid)
            self._log(treelog.branch(
                f"[black bold on green] Password for {name}: "
                f"\"{escape(outcome.psk)}\" [/black bold on green]"))
            try:
                result = save_wps_pbc(ap, outcome.psk)
                if result is None:
                    self._log(treelog.leaf("[dim](save failed)[/dim]"))
                else:
                    self._log(treelog.leaf(_save_line(result)))
            except Exception:
                self._log(treelog.leaf("[dim](save failed)[/dim]"))
        else:
            self._pbc_retry_after = time.monotonic() + _PBC_RETRY_COOLDOWN_S
            self._log(treelog.leaf_warn(
                f"{outcome.result.value} [dim]({escape(outcome.detail)})[/dim], "
                f"retrying in {_PBC_RETRY_COOLDOWN_S:.0f}s while the window's open"))

    def _stop_pbc_capture(self) -> None:
        if self._pbc_campaign is not None:
            self._pbc_campaign.request_stop()
            self._pbc_campaign = None

    def _user_stop_pbc(self) -> None:
        """The transient 'Stop PBC' button."""
        if self._pbc_campaign is None:
            return
        self._pbc_user_stopped = True
        self._pbc_campaign.request_stop()
        self._refresh_buttons()

    # ----- navigation --------------------------------------------------------

    async def action_go_back(self) -> None:
        # Tear down any running attack: Scanner doesn't own the AP's channel, and a forged daemon would keep injecting.
        self._stop_eviltwin()
        self._stop_generate_ivs()
        self._stop_pbc_capture()
        self._stop_wps_pin()
        self._stop_pmkid()
        ap = self._target_ap
        logger.info("[FOCUS] leave: ssid=%r bssid=%s",
                    getattr(ap, "ssid", None), getattr(ap, "bssid", None))
        self.app.pop_screen()
