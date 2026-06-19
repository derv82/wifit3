"""Router endpoint — the right column. Power + signal sit *directly above* the
router art; the ESSID sits *directly below* it (the name labels the router),
then BSSID and the channel. Encryption is NOT shown here — it lives in the log
('Target acquired … WPA2'), the under-sparkline footer, and is implied by the
attack buttons; the channel alone keeps this column uncluttered.

The power line is the live reception-quality meter — the same rainbow
``render_signal_bar`` (beacons/s out of ~9.8) the v1 view uses — widened to fill
the column's negative space, with the dBm flush right. No "Beacons:" prefix: the
bar *is* the readout."""
from __future__ import annotations

import math
import time

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label

from ...signal_bar import render_signal_bar
from .art import BreathingArt, art_size


class RouterEndpoint(Vertical):
    def __init__(self, snap, **kwargs) -> None:
        super().__init__(**kwargs)
        self._snap = snap
        self._width = art_size("focus-ap.ans")[0]      # endpoint column width

    def compose(self) -> ComposeResult:
        yield Label(self._power_line(self._snap), classes="ap-power", id="ap-power")
        yield BreathingArt("focus-ap.ans", classes="endpoint-art")
        yield Label(self._essid_markup(self._snap.ap_essid), classes="ap-essid", id="ap-essid")
        yield Label(self._snap.ap_bssid, classes="ap-static", id="ap-bssid")
        yield Label(f"channel {self._snap.ap_channel}", classes="ap-static", id="ap-chan")

    def update_dynamic(self, snap) -> None:
        """Refresh the power meter (every tick) + the identity facts (cheap; they
        only change on a target switch)."""
        self.query_one("#ap-power", Label).update(self._power_line(snap))
        self.query_one("#ap-essid", Label).update(self._essid_markup(snap.ap_essid))
        self.query_one("#ap-bssid", Label).update(snap.ap_bssid)
        self.query_one("#ap-chan", Label).update(f"channel {snap.ap_channel}")

    @staticmethod
    def _essid_markup(essid: str) -> str:
        """The ESSID as a black-on-cyan chip so it pops as the AP's identity (it
        kept blending in as plain bold white). A cloaked AP stays a dim italic
        marker — no chip on a name we don't have."""
        if essid == "‹hidden›":
            return "[dim italic]‹hidden›[/dim italic]"
        return f"[black bold on cyan] {escape(essid)} [/black bold on cyan]"

    def _power_line(self, snap) -> Text:
        """Rainbow signal bar (left, filling the negative space) + dBm (right).
        ``snap.signal`` is the windowed beacons/s: None=warming, ~0=dead (a
        heartbeat-pulsing ╳)."""
        dbm = f"{snap.power_dbm} dBm"
        bar_w = max(4, self._width - len(dbm) - 1)
        pulse = 0.5 + 0.5 * math.sin(time.time() * math.tau)   # dead-AP heartbeat
        line = Text(no_wrap=True)
        line.append_text(render_signal_bar(snap.signal, width=bar_w, pulse=pulse))
        line.append(" ")
        line.append(dbm)
        return line
