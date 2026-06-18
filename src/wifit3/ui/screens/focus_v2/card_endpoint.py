"""Card endpoint — the left column. The card art, then its static facts
(chipset/driver + the card's own BSSID when the driver exposes it) and the
dynamic line (what the card is doing right now). The attack buttons used to live
here but moved to the top "action area" — the card column is now just identity +
live state, vertically centered against the flow channel."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label

from .art import BreathingArt


class CardEndpoint(Vertical):
    def __init__(self, snap, **kwargs) -> None:
        super().__init__(**kwargs)
        self._snap = snap

    def compose(self) -> ComposeResult:
        yield BreathingArt("focus-card.ans", classes="endpoint-art")
        yield Label(self._snap.card_chipset, classes="card-static")
        if self._snap.card_bssid:
            yield Label(self._snap.card_bssid, classes="card-static")
        if self._snap.card_dynamic:
            yield Label(self._snap.card_dynamic, classes="card-dynamic")
