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

Power + signal live above the router ESSID, not in the top bar. Portrait is
deferred (see ``planning/FOCUS-REDESIGN.md``).

This is the throwaway-able shell: it paints a ``fake_snapshot()`` so the layout
can be judged before any campaign wiring. Selected behind ``WIFIT3_FOCUS_V2=1``
(see ``ui/app.py``); the v1 ``FocusView`` stays the default.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, Static

from ...focus_model import fake_snapshot
from .card_endpoint import CardEndpoint
from .clients_list import ClientsList
from .flow_channel import FlowChannel
from .log_band import LogBand
from .router_endpoint import RouterEndpoint

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
    #clients { width: 40; height: 100%%; border: round $primary; padding: 0 1; }
    .log-line { width: 100%%; height: 1; }

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
        self._snap = fake_snapshot()

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Button("‹ Scanner", id="back")
            for i, label in enumerate(self._snap.buttons):
                yield Button(label, id=f"attack-{i}", classes="attack-btn")
            yield Static("\n".join(self._snap.status), id="status")
        with Horizontal(id="mid"):
            yield CardEndpoint(self._snap, id="card")
            yield FlowChannel(self._snap.flow, id="flow")
            yield RouterEndpoint(self._snap, id="router")
        with Horizontal(id="bottom"):
            yield LogBand(self._snap.log_lines, id="log")
            yield ClientsList(self._snap.clients, id="clients")

    def on_mount(self) -> None:
        self._distribute()

    def on_resize(self) -> None:
        self._distribute()

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
