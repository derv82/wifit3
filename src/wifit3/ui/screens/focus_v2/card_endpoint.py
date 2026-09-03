"""Card endpoint: the left column. The card art, then its static facts
(chipset/driver + the card's own BSSID when the driver exposes it) and the
dynamic line (what the card is doing right now). Just identity + live state (the
attack buttons live in the top "action area"), vertically centered against the
packet dashboard."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label

from .art import BreathingArt
from .tx_picker import TxDevicePicker


class CardEndpoint(Vertical):
    def __init__(self, *, chipset: str = "no card", bssid: str | None = None,
                 dynamic: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._chipset = chipset
        self._bssid = bssid
        self._dynamic = dynamic
        self._last_dynamic: str | None = None      # last-pushed dynamic line; skip no-op repaints
        self._last: dict[str, str] = {}            # last-pushed identity label values; skip no-op repaints

    def compose(self) -> ComposeResult:
        yield BreathingArt("focus-card.ans", classes="endpoint-art")
        # The product-name slot is the TX-device picker: a plain label with one card, a dropdown
        # to pin the injection card with two or more. Driven by sync_picker from the screen tick.
        yield TxDevicePicker(self._chipset, id="tx-picker")
        # Always present (the card MAC is static per card) so a later tick can
        # show/hide it; hidden when the driver doesn't expose its own BSSID.
        bssid = Label(self._bssid or "", classes="card-static", id="card-bssid")
        bssid.display = bool(self._bssid)
        yield bssid
        # The dynamic line ("● replaying" etc) is always composed so update()
        # can toggle it; hidden while the card is idle (passive capture).
        dyn = Label(self._dynamic, classes="card-dynamic", id="card-dynamic")
        dyn.display = bool(self._dynamic)
        yield dyn

    def update(self, *, dynamic: str) -> None:
        """Refresh the live 'what the card is doing' line, only when it changed
        (chipset / BSSID update on their own via ``sync_picker`` / ``update_bssid``).
        Textual's ``Label.update`` refreshes unconditionally, so the change check
        skips the no-op repaint that otherwise fires every tick."""
        if dynamic == self._last_dynamic:
            return
        self._last_dynamic = dynamic
        dyn = self.query_one("#card-dynamic", Label)
        dyn.update(dynamic)
        dyn.display = bool(dynamic)

    def sync_picker(self, members, channel, current, locked: bool) -> None:
        """Refresh the TX-device picker (trigger name + dropdown state) from the live pool."""
        self.query_one(TxDevicePicker).sync(members, channel, current, locked)

    def update_bssid(self, bssid: str | None) -> None:
        """Re-apply the card's own BSSID line. Shows only for a single card (a multi-card pool has
        no single MAC). The pool can change under us (plug/unplug) while Focus is open."""
        if self._push("#card-bssid", bssid or ""):
            self.query_one("#card-bssid", Label).display = bool(bssid)

    def set_art(self, name: str) -> None:
        """Point the card art at ``name`` (BreathingArt.set_art no-ops when unchanged)."""
        self.query_one(BreathingArt).set_art(name)

    def _push(self, sel: str, value: str) -> bool:
        """Update a label only when its value changed; return whether it changed."""
        if self._last.get(sel) == value:
            return False
        self._last[sel] = value
        self.query_one(sel, Label).update(value)
        return True

    def flicker(self) -> None:
        """Pulse the card LED. The screen calls this when we TX a frame."""
        self.query_one(BreathingArt).pulse()
