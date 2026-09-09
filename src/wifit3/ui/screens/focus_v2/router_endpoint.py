"""Router endpoint: the right column. Power + signal sit *directly above* the
router art; the ESSID sits *directly below* it (the name labels the router),
then BSSID and the channel. Encryption is NOT shown here. It lives in the log
('Target acquired … WPA2'), the under-sparkline footer, and is implied by the
attack buttons; the channel alone keeps this column uncluttered.

The power line is the live reception-quality meter: the rainbow
``render_signal_bar`` (beacons/s out of ~9.8), widened to fill the column's
negative space, with the dBm flush right. No "Beacons:" prefix: the
bar *is* the readout."""
from __future__ import annotations

import math
import time

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Label

from ...signal_bar import render_signal_bar
from .art import BreathingArt, art_size


class RouterEndpoint(Vertical):
    class IdentityRequested(Message):
        """User activated the identity details button."""
        def __init__(self, details: str) -> None:
            super().__init__()
            self.details = details

    class ProbeRequested(Message):
        """User clicked probe button to start active probe."""

    class ProbeCancelRequested(Message):
        """User clicked probe button to cancel active probe."""

    def __init__(self, *, essid: str = "", bssid: str = "", channel: int = 0,
                 power_dbm: int = -100, signal: float | None = None,
                 wps: bool = False, has_m1: bool = False,
                 probing: bool = False, probe_disabled: bool = False,
                 identity: str = "", identity_details: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self._essid = essid
        self._bssid = bssid
        self._channel = channel
        self._power_dbm = power_dbm
        self._signal = signal
        self._wps = wps
        self._has_m1 = has_m1
        self._probing = probing
        self._probe_disabled = probe_disabled
        self._identity = identity
        self._identity_details = identity_details
        self._width = art_size("focus-ap.ans")[0]      # endpoint column width
        self._last: dict[str, str] = {}                # last-pushed label value; skip no-op repaints

    def compose(self) -> ComposeResult:
        yield Label(self._power_line(), classes="ap-power", id="ap-power")
        art = BreathingArt("focus-ap.ans", classes="endpoint-art", id="router-art")
        art.tooltip = self._identity_details
        yield art
        yield Label(self._essid_markup(self._essid), classes="ap-essid", id="ap-essid")
        yield Label(self._bssid, classes="ap-static", id="ap-bssid")
        with Horizontal(classes="ap-static", id="ap-identity-row"):
            identity = Button(self._identity_label(), id="ap-identity")
            identity.styles.line_pad = 0
            identity.disabled = self._identity_details is None or not self._wps
            if self._identity_details and self._wps:
                identity.add_class("identity-known")
            yield identity
            probe = Button(self._probe_icon(), id="ap-probe")
            probe.display = self._wps and not self._has_m1
            probe.tooltip = "Cancel WPS/WSC probe" if self._probing else "Probe AP for WPS/WSC attributes"
            probe.disabled = False if self._probing else self._probe_disabled
            yield probe

    def update(self, *, essid: str, bssid: str, channel: int,
               power_dbm: int, signal: float | None,
               wps: bool = False, has_m1: bool = False,
               probing: bool = False, probe_disabled: bool = False,
               identity: str = "", identity_details: str | None = None) -> None:
        """Update live power meter and target endpoint identity state."""
        self._essid, self._bssid, self._channel = essid, bssid, channel
        self._power_dbm, self._signal = power_dbm, signal
        self._wps, self._has_m1 = wps, has_m1
        self._probing, self._probe_disabled = probing, probe_disabled
        self._identity, self._identity_details = identity, identity_details
        self.query_one("#ap-power", Label).update(self._power_line())
        self.query_one("#router-art", BreathingArt).tooltip = identity_details
        self._push("#ap-essid", self._essid_markup(essid))
        self._push("#ap-bssid", bssid)

        show_probe = self._wps and not self._has_m1
        probe_btn = self.query_one("#ap-probe", Button)
        probe_btn.display = show_probe
        if show_probe:
            probe_btn.label = self._probe_icon()
            probe_btn.tooltip = "Cancel WPS/WSC probe" if probing else "Probe AP for WPS/WSC attributes"
            probe_btn.disabled = False if probing else probe_disabled

        self._push("#ap-identity", self._identity_label())
        ident_btn = self.query_one("#ap-identity", Button)
        ident_btn.styles.line_pad = 0
        ident_btn.disabled = identity_details is None or not self._wps
        ident_btn.set_class(bool(identity_details and self._wps), "identity-known")

    def _push(self, sel: str, value: str) -> None:
        """Update the label only when its value changed: skip the no-op repaint."""
        if self._last.get(sel) == value:
            return
        self._last[sel] = value
        widget = self.query_one(sel)
        if isinstance(widget, Button):
            widget.label = value
        else:
            widget.update(value)

    def flicker(self) -> None:
        """Pulse the router LED. The screen calls this on RX from the target."""
        self.query_one(BreathingArt).pulse()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ap-identity" and self._identity_details and self._wps:
            event.stop()
            self.post_message(self.IdentityRequested(self._identity_details))
        elif event.button.id == "ap-probe":
            event.stop()
            if self._probing:
                self.post_message(self.ProbeCancelRequested())
            else:
                self.post_message(self.ProbeRequested())

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        if len(text) <= max_len:
            return text
        return text[:max_len - 1] + "…"

    def _identity_label(self) -> str:
        if not self._wps:
            return f"channel {self._channel}"
        if self._has_m1:
            text = self._identity or f"channel {self._channel}"
            return self._truncate(text, 20)
        text = self._identity or f"ch {self._channel}"
        return self._truncate(text, 16)

    def _probe_icon(self) -> str:
        return "❌" if self._probing else "🔍"

    @staticmethod
    def _essid_markup(essid: str) -> str:
        """The ESSID as a black-on-cyan chip so it pops as the AP's identity (it
        kept blending in as plain bold white). A cloaked AP stays a dim italic
        marker: no chip on a name we don't have."""
        if essid == "‹hidden›":
            return "[dim italic]‹hidden›[/dim italic]"
        return f"[black bold on cyan] {escape(essid)} [/black bold on cyan]"

    def _power_line(self) -> Text:
        """Rainbow signal bar (left, filling the negative space) + dBm (right).
        ``self._signal`` is the windowed beacons/s: None=warming, ~0=dead (a
        heartbeat-pulsing ╳)."""
        dbm = f"{self._power_dbm} dBm"
        bar_w = max(4, self._width - len(dbm) - 1)
        pulse = 0.5 + 0.5 * math.sin(time.time() * math.tau)   # dead-AP heartbeat
        line = Text(no_wrap=True)
        line.append_text(render_signal_bar(self._signal, width=bar_w, pulse=pulse))
        line.append(" ")
        line.append(dbm)
        return line
