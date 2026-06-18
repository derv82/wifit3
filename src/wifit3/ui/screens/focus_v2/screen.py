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

Data flow: the shared ``focus_model.build_snapshot`` derives a per-tick
``FocusSnapshot`` from ``(ap, iface, campaigns)``; this screen paints it. With no
target (the geometry tests, the ``shoot_focus_v2`` screenshots) it falls back to
``fake_snapshot()`` so the layout is still fully populated. Attack BUTTONS are
inert in this step — wiring their triggers is a follow-on. Selected behind
``WIFIT3_FOCUS_V2=1`` (see ``ui/app.py``); the v1 ``FocusView`` stays default.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Set

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Static

from wifit3.engine.attacks import treelog
from wifit3.engine.save import save_handshake, save_pmkid

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


class FocusViewV2(Screen):
    # Width-tier hook for later responsive / portrait work. The shell keeps a
    # fixed clients column + fluid log/flow, so nothing varies on these yet.
    HORIZONTAL_BREAKPOINTS = [(0, "-compact"), (100, "-normal"), (140, "-wide")]

    BINDINGS = [
        Binding("escape", "go_back", "Back to Scanner", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

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
        # Campaign handles (all idle in this step — attack buttons aren't wired
        # yet). Held so the snapshot/headline derivations read them and so the
        # follow-on button-wiring step has the slots ready.
        self._wep_campaign = None
        self._wps_campaign = None
        self._wpa3_down_attack = None
        self._pbc_task = None

    # ----- compose -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Build from the live target if one is set (real app), else the demo
        # snapshot (geometry tests / screenshots). self.app is available here.
        self._snap = self._snapshot()
        with Horizontal(id="topbar"):
            yield Button("‹ Scanner", id="back")
            for i, label in enumerate(self._snap.buttons):
                yield Button(label, id=f"attack-{i}", classes="attack-btn")
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

    def _campaigns(self) -> fm.Campaigns:
        pbc_busy = self._pbc_task is not None and not self._pbc_task.done()
        return fm.Campaigns(
            wep=self._wep_campaign, wps=self._wps_campaign,
            wpa3_down=self._wpa3_down_attack, pbc_busy=pbc_busy,
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

    # ----- target (re)acquisition --------------------------------------------

    async def _enter_target(self) -> None:
        """Bind to ``app.target_ap``: reset per-target state, re-point the flow
        channel + clients + endpoints, tune the radio, and seed the log. A no-op
        (leaving the demo content) when there's no target."""
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
        snap = self._snapshot()
        self._snap = snap
        self.query_one("#status", Static).update(self._render_status(snap.status))
        self.query_one("#card", CardEndpoint).update_dynamic(snap)
        self.query_one("#router", RouterEndpoint).update_dynamic(snap)
        self.query_one("#clients", ClientsList).sync(snap.clients)
        # The flow channel self-samples on its own timer (bound in _enter_target).
        iface = getattr(self.app, "active_interface", None)
        self._drain_capture_events(self._target_ap, iface.forged_macs if iface else set())

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

    # ----- navigation --------------------------------------------------------

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        # Attack + per-client deauth buttons are inert in this step (wiring the
        # triggers is a follow-on). Only navigation is live.
        if event.button.id == "back":
            await self.action_go_back()

    async def action_go_back(self) -> None:
        self.app.pop_screen()
