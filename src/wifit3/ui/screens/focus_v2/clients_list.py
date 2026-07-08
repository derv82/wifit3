"""Clients list — bottom-right, bordered. Broadcast deauth pinned at the top,
then one compact left-aligned row per client: BSSID · power · packets · an
inline ``✕`` (white-on-red) that deauths just that client — no select-then-act.

Rows sync live: :meth:`ClientsList.sync` reconciles the rows to the current
client set each tick — add newly-seen, update power/packets, drop departed (a
client that left, or the previous target's clients after a switch). It's
idempotent and never removes-then-remounts the same id in one pass, so it's safe
across target switches and screen re-entry (no reset that races Textual's async
widget removal). Each ``✕`` carries the client's BSSID so the screen's button
handler knows whom to deauth."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
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
        # deauth button id -> client mac, so the screen's handler knows whom to
        # deauth from the inline ✕ that was clicked.
        self._by_button: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        # Broadcast button pinned at the top; only the row list scrolls below it.
        yield Button("Deauth all", id="deauth-all", classes="bcast-btn")
        yield VerticalScroll(
            *(self._make_row(c.bssid, c.power, c.packets) for c in self._clients),
            id="client-rows",
        )

    def on_mount(self) -> None:
        self._update_title()

    def _rows_host(self) -> VerticalScroll:
        """The scroll container the client rows live in (new rows mount here)."""
        return self.query_one("#client-rows", VerticalScroll)

    # ----- live sync ---------------------------------------------------------

    def sync(self, clients) -> None:
        """Reconcile rows to ``clients``: drop departed, update known, add new.

        Diffing against ``_known`` (rather than clearing + re-adding) keeps
        re-entry to the same target a no-op for unchanged rows, which is what
        avoids the DuplicateIds crash: ``row.remove()`` is async in Textual, so a
        clear-then-readd of the same id can collide with the still-pending
        removal. The add path also guards against a row whose removal hasn't yet
        landed (a flapping client)."""
        current = {c.bssid for c in clients}
        for mac in list(self._known):
            if mac not in current:
                self._remove_row(mac)
        for c in clients:
            refs = self._known.get(c.bssid)
            if refs is None:
                # Skip if a same-id row is still mid-removal — it mounts cleanly
                # next tick once the removal lands, rather than duplicating the id.
                if self.query(f"#{_row_id(c.bssid)}"):
                    continue
                self._rows_host().mount(self._make_row(c.bssid, c.power, c.packets))
            else:
                pwr, pkts = refs
                pwr.update(str(c.power))
                pkts.update(str(c.packets))
        self._update_title()

    def _remove_row(self, mac: str) -> None:
        rid = _row_id(mac)
        try:
            self.query_one(f"#{rid}").remove()
        except Exception:
            pass
        self._known.pop(mac, None)
        self._by_button.pop(f"{rid}-deauth", None)

    def client_mac(self, button_id: str) -> str | None:
        """The client MAC behind an inline-deauth ✕ button id (None if unknown)."""
        return self._by_button.get(button_id)

    def set_deauth_enabled(self, enabled: bool) -> None:
        """Enable/disable every deauth control at once — the broadcast button and
        each per-client ✕ (the only Buttons this list owns). Greyed when a
        PMF-Required AP would refuse the deauth, or another attack owns the radio."""
        disabled = not enabled
        for btn in self.query(Button):
            btn.disabled = disabled

    # The pinned 'Deauth all' button is always visible (composed): a broadcast
    # deauth to ff:ff:ff:ff:ff:ff is valid even with no *known* clients — it hits
    # every associated STA. It's greyed, not hidden, when deauth is blocked
    # (PMF-Required), via set_deauth_enabled above.

    # ----- helpers -----------------------------------------------------------

    def _make_row(self, mac: str, power: int, packets: int) -> Horizontal:
        pwr = Label(str(power), classes="cl-pwr")
        pkts = Label(str(packets), classes="cl-pkts")
        self._known[mac] = (pwr, pkts)
        btn_id = f"{_row_id(mac)}-deauth"
        self._by_button[btn_id] = mac
        return Horizontal(
            Label(mac, classes="cl-bssid"),
            pwr, pkts,
            Button("✕", id=btn_id, classes="cl-deauth"),
            classes="client-row", id=_row_id(mac),
        )

    def _update_title(self) -> None:
        self.border_title = f"CLIENTS ({len(self._known)})"
