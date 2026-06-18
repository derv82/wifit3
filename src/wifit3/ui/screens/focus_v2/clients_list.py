"""Clients list — bottom-right, bordered. Broadcast deauth pinned at the top,
then one compact left-aligned row per client: BSSID · power · packets · an
inline ``✕`` (white-on-red) that deauths just that client — no select-then-act.

Rows sync live: :meth:`ClientsList.sync` adds newly-seen clients and updates the
power/packets of known ones each tick; :meth:`reset` clears them on a target
switch. Each ``✕`` carries the client's BSSID so the screen's button handler
knows whom to deauth (wired in a later step)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label


def _row_id(mac: str) -> str:
    """A widget-id-safe token for a client row (ids can't contain ':')."""
    return "cl-" + mac.replace(":", "")


class ClientsList(Vertical):
    def __init__(self, clients, **kwargs) -> None:
        super().__init__(**kwargs)
        self._clients = clients
        # mac -> (pwr_label, pkts_label) for in-place updates without re-mounting.
        self._known: dict[str, tuple[Label, Label]] = {}

    def compose(self) -> ComposeResult:
        yield Button("Deauth all", id="deauth-all", classes="bcast-btn")
        for c in self._clients:
            yield self._make_row(c.bssid, c.power, c.packets)

    def on_mount(self) -> None:
        self._update_title()

    # ----- live sync ---------------------------------------------------------

    def sync(self, clients) -> None:
        """Add rows for newly-seen clients, update power/packets on known ones."""
        for c in clients:
            refs = self._known.get(c.bssid)
            if refs is None:
                self.mount(self._make_row(c.bssid, c.power, c.packets))
            else:
                pwr, pkts = refs
                pwr.update(str(c.power))
                pkts.update(str(c.packets))
        self._update_title()

    def reset(self) -> None:
        """Drop every client row (target switch). The broadcast button stays."""
        for row in self.query(".client-row"):
            row.remove()
        self._known.clear()
        self._update_title()

    # ----- helpers -----------------------------------------------------------

    def _make_row(self, mac: str, power: int, packets: int) -> Horizontal:
        pwr = Label(str(power), classes="cl-pwr")
        pkts = Label(str(packets), classes="cl-pkts")
        self._known[mac] = (pwr, pkts)
        return Horizontal(
            Label(mac, classes="cl-bssid"),
            pwr, pkts,
            Button("✕", id=f"{_row_id(mac)}-deauth", classes="cl-deauth"),
            classes="client-row", id=_row_id(mac),
        )

    def _update_title(self) -> None:
        self.border_title = f"CLIENTS ({len(self._known)})"
