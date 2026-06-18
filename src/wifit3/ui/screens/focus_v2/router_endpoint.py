"""Router endpoint — the right column. Power + signal sit *directly above* the
router art; the ESSID sits *directly below* it (the name labels the router),
then the static facts: BSSID and ``ch · encryption``. Splitting the ESSID away
from the power line spreads the labels out instead of clustering them above the
art (which reads as a cramped block surrounded by negative space)."""
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
        yield Label(self._power_text(), classes="ap-power")       # directly above the router
        yield BreathingArt("focus-ap.ans", classes="endpoint-art")
        yield Label(self._snap.ap_essid, classes="ap-essid")      # directly below the router
        yield Label(self._snap.ap_bssid, classes="ap-static")
        yield Label(
            f"ch {self._snap.ap_channel} · {self._snap.ap_encryption}",
            classes="ap-static",
        )

    def _power_text(self) -> str:
        return f"▂▄▅█ {self._snap.power_dbm} dBm"
