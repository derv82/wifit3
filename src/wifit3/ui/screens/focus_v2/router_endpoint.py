"""Router endpoint — the right column. Power + signal sit *directly above the
ESSID* (correlating signal with the named target), then the router art, then the
static facts: BSSID and ``ch · encryption``."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label

from .art import BreathingArt


class RouterEndpoint(Vertical):
    def __init__(self, snap, **kwargs) -> None:
        super().__init__(**kwargs)
        self._snap = snap

    def compose(self) -> ComposeResult:
        yield Label(self._power_text(), classes="ap-power")
        yield Label(self._snap.ap_essid, classes="ap-essid")
        yield BreathingArt("focus-ap.ans", classes="endpoint-art")
        yield Label(self._snap.ap_bssid, classes="ap-static")
        yield Label(
            f"ch {self._snap.ap_channel} · {self._snap.ap_encryption}",
            classes="ap-static",
        )

    def _power_text(self) -> str:
        return f"▂▄▅█ {self._snap.power_dbm} dBm"
