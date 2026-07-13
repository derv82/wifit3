"""Card endpoint — the left column. The card art, then its static facts
(chipset/driver + the card's own BSSID when the driver exposes it) and the
dynamic line (what the card is doing right now). Just identity + live state (the
attack buttons live in the top "action area"), vertically centered against the
flow channel."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label

from .art import BreathingArt


class CardEndpoint(Vertical):
    def __init__(self, snap, **kwargs) -> None:
        super().__init__(**kwargs)
        self._snap = snap
        self._last_dynamic: str | None = None      # last-pushed dynamic line; skip no-op repaints

    def compose(self) -> ComposeResult:
        yield BreathingArt("focus-card.ans", classes="endpoint-art")
        yield Label(self._snap.card_chipset, classes="card-static", id="card-chipset")
        # Always present (the card MAC is static per card) so a later tick can
        # show/hide it; hidden when the driver doesn't expose its own BSSID.
        bssid = Label(self._snap.card_bssid or "", classes="card-static", id="card-bssid")
        bssid.display = bool(self._snap.card_bssid)
        yield bssid
        # The dynamic line ("● replaying" etc) is always composed so update_dynamic
        # can toggle it; hidden while the card is idle (passive capture).
        dyn = Label(self._snap.card_dynamic, classes="card-dynamic", id="card-dynamic")
        dyn.display = bool(self._snap.card_dynamic)
        yield dyn

    def update_dynamic(self, snap) -> None:
        """Refresh the live 'what the card is doing' line — only when it changed.
        Identity (chipset / BSSID) is static per card, set once at compose.
        Textual's ``Label.update`` refreshes unconditionally, so guarding skips
        the no-op repaint that otherwise fires every tick."""
        if snap.card_dynamic == self._last_dynamic:
            return
        self._last_dynamic = snap.card_dynamic
        dyn = self.query_one("#card-dynamic", Label)
        dyn.update(snap.card_dynamic)
        dyn.display = bool(snap.card_dynamic)

    def flicker(self) -> None:
        """Pulse the card LED — the screen calls this when we TX a frame."""
        self.query_one(BreathingArt).pulse()
