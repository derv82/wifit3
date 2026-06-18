"""``FocusViewV2`` — the spatial "router-admin" Focus redesign (landscape v1).

Layout (top to bottom):
- **Top bar** (fixed height): an "action area" on the left — the back button +
  the encryption-conditional attack buttons, all the clickables in one place —
  then the status line, expanding to fill and stay centered.
- **Mid band**: card | flow-channel | router. Card and router are fixed-width
  (the art is 20 cells) and vertically centered; the flow channel fills the
  middle. The band's height is capped so the sparklines reach full 2-row height
  and the endpoint columns fit, then extra terminal height flows to the bottom.
- **Bottom band**: LOG (fluid width) | CLIENTS (fixed width). Grows once the mid
  band is satisfied, so tall terminals show more log lines + clients.

Power + signal live above the router ESSID (the live rainbow signal bar), not in
the top bar. Portrait is deferred (see ``planning/FOCUS-REDESIGN.md``).

Data flow: the shared ``focus_model`` derivations drive everything — a per-tick
``build_snapshot`` paints the regions, ``derive_buttons`` drives the conditional
attack buttons. With no target (the geometry tests) it falls back to
``fake_snapshot()`` so the layout stays populated.

The campaign brains are shared via ``focus_model``; the *screen-side* attack
handlers + campaign lifecycle are duplicated per-view by design (the log/save/
teardown side effects are too entangled with the widgets to share cheaply, and
keeping them separate lets v2 evolve without touching v1). The agent wires the
TX paths; firing live deauth/inject is the user's explicit action. Selected
behind ``WIFIT3_FOCUS_V2=1`` (see ``ui/app.py``); v1 ``FocusView`` stays default.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional, Set

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Static

from wifit3.engine.attacks import treelog
from wifit3.engine.attacks.pmkid_harvest import PmkidHarvestAttack
from wifit3.engine.attacks.wep.campaign import WepCampaign
from wifit3.engine.attacks.wpa3_downgrade import WPA3DowngradeAttack
from wifit3.engine.attacks.wps.campaign import WpsCampaign
from wifit3.engine.attacks.wps.pbc import WpsPbcCapture
from wifit3.engine.attacks.wps.registrar import PinResult
from wifit3.engine.save import (
    save_handshake, save_pmkid, save_wep_key, save_wps_pbc, save_wps_pin,
)

from ... import focus_model as fm
from ...capture_events import (
    DECLOAK_METHOD_LABELS, CaptureEvent, CaptureEventDetector, CaptureKind,
)
from ...capture_log import eapol_message_markup, short_sta
from .card_endpoint import CardEndpoint
from .clients_list import ClientsList
from .flow_channel import FlowChannel
from .log_band import LogBand
from .router_endpoint import RouterEndpoint

logger = logging.getLogger(__name__)

_ENDPOINT_W = 20          # the .ans art is exactly 20 cells wide
_TOPBAR_H = 3
# Mid band caps once the sparklines hit full 2-row height and the endpoint
# columns fit; beyond that, extra height flows to the bottom band. The bottom
# floor keeps >= 3 client rows visible even on short terminals.
_CENTER_MAX = 13
_CENTER_MIN = 7
_BOTTOM_MIN = 6
# Horizontal breathing room on the mid row: none up to ~80 cols, then ramped so a
# wide terminal centers card | flow | router with side margins instead of
# stretching them to the edges (~40 cols/side by ~180 wide). Bottom stays full.
_PAD_START = 80
_PAD_RATE = 0.4

# The full attack-button set (stable ids, shared with v1). All are composed once;
# derive_buttons toggles visibility/enablement/label/variant per target + tick.
_ATTACK_BUTTONS = [
    ("btn-gen-ivs", "Replay"), ("btn-chop", "Chop"), ("btn-pmkid", "PMKID"),
    ("btn-wps-pin", "WPS PIN"), ("btn-wpa3-down", "WPA ↓"),
]


class FocusViewV2(Screen):
    # Width-tier hook for later responsive / portrait work. The shell keeps a
    # fixed clients column + fluid log/flow, so nothing varies on these yet.
    HORIZONTAL_BREAKPOINTS = [(0, "-compact"), (100, "-normal"), (140, "-wide")]

    BINDINGS = [
        Binding("escape", "go_back", "Back to Scanner", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

    # Deauth pairs per selected client, round-robin'd so each frame pair is
    # followed by an RX window (matches v1; keeps the half-duplex radio from
    # TX-saturating when many clients are queued).
    _DEAUTH_SEL_ROUNDS = 10

    CSS = """
    FocusViewV2 { layout: vertical; background: $surface; }

    #topbar { height: %(top)d; }
    #topbar Button { height: 3; width: auto; min-width: 0; margin: 0 1 0 0; }
    .attack-btn { background: $primary; color: $text; }
    #status { width: 1fr; height: 3; content-align: center middle; text-align: center; }

    #mid { height: 1fr; }
    #card, #router { width: %(ew)d; align: center middle; }
    /* Router art is a row shorter than the card's, so bottom-align it: the freed
       row sits above the power line (de-clustering it) and the router's BSSID/
       channel line up with the card's bssid/dynamic row. */
    #router { align: center bottom; }
    #flow { width: 1fr; height: 100%%; padding: 0 1; }
    .endpoint-art { width: %(ew)d; background: transparent; }
    .card-static, .ap-static { width: 100%%; height: 1; text-align: center; color: $text-muted; }
    .card-dynamic { width: 100%%; height: 1; text-align: center; color: $accent; }
    .ap-essid { width: 100%%; height: 1; text-align: center; text-style: bold; }
    .ap-power { width: 100%%; height: 1; text-align: center; }

    #bottom { height: 1fr; }
    #log { width: 1fr; height: 100%%; border: round $primary; padding: 0 1; }
    #log-rich { width: 100%%; height: 1fr; background: transparent; border: none; padding: 0; }
    #clients { width: 40; height: 100%%; border: round $primary; padding: 0 1; }

    .bcast-btn { width: 100%%; height: 1; min-width: 0; border: none; margin: 0 0 1 0;
                 background: $error; color: $text; content-align: center middle; }
    .client-row { height: 1; width: 100%%; }
    .cl-bssid { width: 17; }
    .cl-pwr { width: 5; text-align: right; }
    .cl-pkts { width: 6; text-align: right; }
    .cl-deauth { width: 3; min-width: 3; height: 1; border: none; margin: 0 0 0 1;
                 background: red; color: white; content-align: center middle; }
    """ % {"ew": _ENDPOINT_W, "top": _TOPBAR_H}

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._snap = fm.fake_snapshot()
        self._target_ap = None
        # Windowed beacon-rate samples (caller-owned; see focus_model.beacon_rate).
        self._beacon_samples: deque = deque()
        # Granular: surfaces every new EAPOL frame, not just completions.
        self._events = CaptureEventDetector(granular_eapol=True)
        self._tick_timer = None
        # Live campaign handles — held so the toggle buttons can Start/Stop and so
        # target/screen transitions tear them down deterministically.
        self._wep_campaign: Optional[WepCampaign] = None
        self._wps_campaign: Optional[WpsCampaign] = None
        self._wpa3_down_attack: Optional[WPA3DowngradeAttack] = None
        self._pbc_task: Optional[asyncio.Task] = None

    # ----- compose -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Build from the live target if one is set (real app), else the demo
        # snapshot (geometry tests). self.app is available here.
        self._snap = self._snapshot()
        with Horizontal(id="topbar"):
            yield Button("‹ Scanner", id="back")
            # The full attack set is composed once (hidden); derive_buttons shows
            # only the ones that fit the target and drives their label/variant.
            for bid, label in _ATTACK_BUTTONS:
                btn = Button(label, id=bid, classes="attack-btn")
                btn.display = False
                yield btn
            yield Static(self._render_status(self._snap.status), id="status")
        with Horizontal(id="mid"):
            yield CardEndpoint(self._snap, id="card")
            yield FlowChannel(self._snap.flow, id="flow")
            yield RouterEndpoint(self._snap, id="router")
        with Horizontal(id="bottom"):
            yield LogBand(self._snap.log_lines, id="log")
            yield ClientsList(self._snap.clients, id="clients")

    async def on_mount(self) -> None:
        self._tick_timer = self.set_interval(1 / 10, self._tick)
        self._distribute()
        await self._enter_target()

    async def on_screen_resume(self) -> None:
        await self._enter_target()

    def on_resize(self) -> None:
        self._distribute()

    # ----- snapshot building -------------------------------------------------

    def _pbc_busy(self) -> bool:
        return self._pbc_task is not None and not self._pbc_task.done()

    def _campaigns(self) -> fm.Campaigns:
        return fm.Campaigns(
            wep=self._wep_campaign, wps=self._wps_campaign,
            wpa3_down=self._wpa3_down_attack, pbc_busy=self._pbc_busy(),
        )

    def _snapshot(self) -> fm.FocusSnapshot:
        ap = getattr(self.app, "target_ap", None)
        if ap is None:
            return fm.fake_snapshot()
        iface = getattr(self.app, "active_interface", None)
        return fm.build_snapshot(
            ap, iface, self._campaigns(), self._beacon_samples, time.time())

    @staticmethod
    def _render_status(status) -> Text:
        return Text("\n").join(Text.from_markup(s, emoji=False) for s in status)

    def _apply_button(self, selector: str, state: fm.ButtonState) -> None:
        btn = self.query_one(selector, Button)
        btn.display = state.visible
        btn.disabled = state.disabled
        btn.label = state.label
        btn.variant = state.variant

    def _refresh_buttons(self) -> None:
        """Drive the conditional attack buttons from derive_buttons (no-op when
        there's no target — the geometry tests leave them composed-hidden)."""
        ap = getattr(self.app, "target_ap", None)
        if ap is None:
            return
        st = fm.derive_buttons(ap, self._campaigns())
        self._apply_button("#btn-gen-ivs", st.gen_ivs)
        self._apply_button("#btn-chop", st.chop)
        self._apply_button("#btn-pmkid", st.pmkid)
        self._apply_button("#btn-wps-pin", st.wps_pin)
        self._apply_button("#btn-wpa3-down", st.wpa3_down)

    # ----- target (re)acquisition --------------------------------------------

    async def _enter_target(self) -> None:
        """Bind to ``app.target_ap``: tear down the previous target's campaigns
        (their forged templates are bound to the old BSSID), reset per-target
        state, re-point the flow channel + clients + endpoints, tune the radio,
        and seed the log. A no-op (leaving the demo content) with no target."""
        # Any running campaign's payload is bound to the PREVIOUS target — stop
        # them before switching so we never inject the wrong AP.
        self._stop_wpa3_down()
        self._stop_generate_ivs()
        self._stop_pbc_capture()
        self._stop_wps_pin()

        ap = getattr(self.app, "target_ap", None)
        self._target_ap = ap
        if ap is None:
            return
        iface = getattr(self.app, "active_interface", None)

        self._beacon_samples.clear()
        self._events.reset()
        self.query_one("#clients", ClientsList).reset()
        self.query_one("#log", LogBand).clear()

        snap = self._snapshot()
        self._snap = snap
        self.query_one("#flow", FlowChannel).reconfigure(snap.flow, iface, ap.bssid)
        self.query_one("#card", CardEndpoint).update_dynamic(snap)
        self.query_one("#router", RouterEndpoint).update_dynamic(snap)
        self.query_one("#status", Static).update(self._render_status(snap.status))
        self._refresh_buttons()

        # Log acquisition + tune the radio to the pinned target channel.
        if ap.ssid:
            chip = f"[black bold on cyan] {escape(ap.ssid)} [/black bold on cyan]"
            self._log(f"[bold]Target acquired:[/bold] {chip}")
        else:
            self._log("[bold]Target acquired:[/bold] "
                      "[dim italic]cloaked network — hidden SSID[/dim italic]")
        self._log(treelog.branch(f"[dim]BSSID:[/dim] [white]{ap.bssid}[/white]"))
        if iface:
            try:
                ok = await iface.set_channel(ap.channel, scan=False)
            except Exception:
                logger.exception("Focus v2 channel tune failed")
                ok = False
            if ok:
                self._log(treelog.leaf(f"Tuned to [cyan]channel {ap.channel}[/cyan]"))
            else:
                self._log(treelog.leaf(
                    f"[yellow]Tried to tune to channel {ap.channel}[/yellow]"))
        else:
            self._log(treelog.leaf("[yellow]no interface — passive view only[/yellow]"))

        if ap.pmf_required:
            self._log("[bold yellow]PMF Required:[/] "
                      "AP requires [bold]Protected Management Frames[/]")

    # ----- per-tick paint ----------------------------------------------------

    def _tick(self) -> None:
        if self._target_ap is None or not self.is_current:
            return
        ap = self._target_ap

        # Campaign lifecycle side effects (kept here, not in focus_model): close a
        # finished WPS-PIN campaign, auto-save + tear down a cracked WEP campaign,
        # and opportunistically grab an open WPS push-button window.
        if self._wps_campaign is not None and self._wps_campaign.state.phase == "done":
            self._stop_wps_pin()
        if self._wep_campaign is not None and self._wep_campaign.recovered_key is not None:
            result = save_wep_key(ap, self._wep_campaign.recovered_key)
            if result is not None:
                verb = "saved" if result.was_new else "already saved as"
                self._log(f"[dim]({verb} {escape(result.path.name)})[/dim]")
            self._stop_generate_ivs()
        if (ap.wps_pbc_active and not self._pbc_busy() and not ap.has_psk
                and self._wep_campaign is None and self._wpa3_down_attack is None
                and self._wps_campaign is None):
            self._pbc_task = asyncio.create_task(self._auto_capture_pbc(ap))

        snap = self._snapshot()
        self._snap = snap
        self.query_one("#status", Static).update(self._render_status(snap.status))
        self.query_one("#card", CardEndpoint).update_dynamic(snap)
        self.query_one("#router", RouterEndpoint).update_dynamic(snap)
        self.query_one("#clients", ClientsList).sync(snap.clients)
        self._refresh_buttons()
        # The flow channel self-samples on its own timer (bound in _enter_target).
        iface = getattr(self.app, "active_interface", None)
        self._drain_capture_events(ap, iface.forged_macs if iface else set())

    def _distribute(self) -> None:
        """Fill the mid band to full 2-row sparklines (capped at _CENTER_MAX),
        reserving a floor for the bottom band, then pour any extra height into the
        bottom (log + clients grow). Also ramp horizontal padding onto the mid row
        on wide terminals so the endpoints aren't glued to the edges — the bottom
        band stays full width."""
        avail = max(1, self.size.height - _TOPBAR_H)
        center = min(_CENTER_MAX, max(_CENTER_MIN, avail - _BOTTOM_MIN))
        center = max(1, min(center, avail - 1))
        mid = self.query_one("#mid")
        mid.styles.height = center
        self.query_one("#bottom").styles.height = avail - center
        pad = max(0, round((self.size.width - _PAD_START) * _PAD_RATE))
        mid.styles.padding = (0, pad, 0, pad)

    # ----- event log (capture pipeline, duplicated from v1) ------------------

    def _drain_capture_events(self, ap, forged_macs: Set[str]) -> None:
        for ev in self._events.poll(ap, forged_macs=forged_macs):
            self._log_capture_event(ev, ap)

    def _log_capture_event(self, ev: CaptureEvent, ap) -> None:
        if ev.kind == CaptureKind.EAPOL:
            # Per-frame trace: one line per M1-M4 as it lands; the solid-highlight
            # banner is reserved for the completion lines below.
            self._log(eapol_message_markup(ev))
        elif ev.kind == CaptureKind.HANDSHAKE:
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
        elif bid.endswith("-deauth"):
            mac = self.query_one("#clients", ClientsList).client_mac(bid)
            if mac:
                self.run_worker(self._run_deauth_selected(mac), exclusive=True)
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

    # ----- deauth ------------------------------------------------------------

    async def _run_deauth_broadcast(self) -> None:
        """Worker: broadcast-deauth every station associated with the focused AP."""
        ap = self._target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — aborting Broadcast.[/red]")
            return
        BROADCAST = "ff:ff:ff:ff:ff:ff"
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

    async def _run_deauth_selected(self, mac: str) -> None:
        """Worker: deauth a specific client (the inline ✕ that was clicked)."""
        ap = self._target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — aborting Deauth.[/red]")
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
        self._log(treelog.leaf_ok(
            "[bold green]Sent 20 deauth frames[/bold green] [dim](10× client + AP)[/dim]"
        ))

    # ----- PMKID -------------------------------------------------------------

    async def _run_pmkid_harvest(self) -> None:
        """Worker: run a PMKID harvest against the focused AP."""
        ap = self._target_ap
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

    # ----- WPA3 downgrade ----------------------------------------------------

    def _toggle_wpa3_down(self) -> None:
        if self._wpa3_down_attack:
            self._stop_wpa3_down()
        else:
            self._start_wpa3_down()
        self._refresh_buttons()

    def _start_wpa3_down(self) -> None:
        ap = self._target_ap
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — cannot start WPA3 Down.[/red]")
            return
        if not ap.ssid:
            self._log("[yellow]⚠ Cannot run WPA3 Down on a hidden AP — "
                      "SSID unknown, no probe-response payload to forge.[/yellow]")
            return
        if not ap.transition_mode:
            self._log("[yellow]⚠ Target is pure WPA3 (no WPA2 fallback advertised) — "
                      "downgrade not possible.[/yellow]")
            return
        try:
            self._wpa3_down_attack = WPA3DowngradeAttack(iface, ap)
            self._wpa3_down_attack.start()
        except Exception as exc:
            logger.exception("WPA3 Down start failed")
            self._log(f"[bold red]✗ WPA3 Down failed to start:[/bold red] {escape(str(exc))}")
            self._wpa3_down_attack = None
            return
        self._log(f"[bold cyan]WPA3 Downgrade ACTIVE[/bold cyan] on [bold]{escape(ap.ssid)}[/bold]")
        self._log(treelog.branch(
            "[dim]responding to probe requests with[/dim] [white bold]WPA2-only[/white bold]"))
        self._log(treelog.leaf("[dim](reconnects take minutes–hours)[/dim]"))

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
            f"{fm.format_duration(duration)}{failed}[/dim]"))
        self._log(treelog.leaf(
            f"[dim]Probe requests: {stats.directed_probes} directed, "
            f"{stats.wildcard_probes} wildcard[/dim]"))

    # ----- WEP: Generate IVs (Replay) + Chop ---------------------------------

    def _toggle_generate_ivs(self) -> None:
        camp = self._wep_campaign
        if camp is not None and camp.recovered_key is None:
            self._stop_generate_ivs()
        else:
            if camp is not None:          # a finished campaign still around — clear it
                self._stop_generate_ivs()
            self._start_generate_ivs()
        self._refresh_buttons()

    def _start_generate_ivs(self) -> None:
        ap = self._target_ap
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
        self._wep_campaign.stop()
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
        iface = getattr(self.app, "active_interface", None)
        if not ap or not iface:
            self._log("[red]✗ No target / interface — cannot start WPS PIN.[/red]")
            return
        try:
            name = escape(ap.ssid or ap.bssid)
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
        asyncio.create_task(camp.stop())
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
                    verb = "saved" if result.was_new else "already saved as"
                    self._log(treelog.leaf(f"[dim]({verb} {escape(result.path.name)})[/dim]"))
            except Exception:
                self._log(treelog.leaf("[dim](save failed)[/dim]"))
        else:
            self._log(treelog.leaf(
                f"[yellow]WPS PIN stopped[/yellow] "
                f"[dim]({camp.state.tested} tested, phase {camp.state.phase})[/dim]"))

    # ----- WPS PBC auto-capture ----------------------------------------------

    async def _auto_capture_pbc(self, ap) -> None:
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
        if self._pbc_task is not None:
            self._pbc_task.cancel()
            self._pbc_task = None

    # ----- navigation --------------------------------------------------------

    async def action_go_back(self) -> None:
        # Tear down any running attack — Scanner doesn't own the AP's channel and
        # a forged daemon would keep injecting / its RX callback firing forever.
        self._stop_wpa3_down()
        self._stop_generate_ivs()
        self._stop_pbc_capture()
        self._stop_wps_pin()
        self.app.pop_screen()
