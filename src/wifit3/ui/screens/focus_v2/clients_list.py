"""Clients list: bottom-right, bordered. Broadcast deauth pinned at the top,
then one compact left-aligned row per client: an optional device-class emoji
(``wlan.fingerprint``, tooltip carries the full label) · BSSID · power ·
packets · an inline ``✕`` (white-on-red) that deauths just that client: no
select-then-act. Clicking the emoji or the MAC of a fingerprinted client pops
up :class:`FingerprintDetail` with the full label, OUI, and confidence.

Rows sync live: :meth:`ClientsList.sync` reconciles the rows to the current
client set each tick: add newly-seen, update power/packets, drop departed (a
client that left, or the previous target's clients after a switch). It's
idempotent and never removes-then-remounts the same id in one pass, so it's safe
across target switches and screen re-entry (no reset that races Textual's async
widget removal). Each ``✕`` carries the client's BSSID so the screen's button
handler knows whom to deauth."""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from wifit3.wlan.fingerprint import Fingerprint


def _row_id(mac: str) -> str:
    """A widget-id-safe token for a client row (ids can't contain ':')."""
    return "cl-" + mac.replace(":", "")


class FingerprintDetail(ModalScreen[None]):
    """A small popup near the clicked row: the full fingerprint label, the matched OUI, and its
    confidence tier. Dismiss by clicking the backdrop (anywhere outside the box) or Escape."""

    BINDINGS = [Binding("escape", "dismiss", "Close", show=False)]

    DEFAULT_CSS = """
    FingerprintDetail { background: $background 0%; }
    FingerprintDetail > #fp-box {
        width: auto; height: auto; border: round $accent; background: $panel; padding: 0 1;
    }
    """

    def __init__(self, mac: str, fp: Fingerprint, *, offset: tuple[int, int]) -> None:
        super().__init__()
        self._mac = mac
        self._fp = fp
        self._offset = offset

    def compose(self) -> ComposeResult:
        box = Vertical(
            Label(f"{self._fp.emoji} {self._fp.label}"),
            Label(f"[dim]OUI: {self._mac[:8]}[/dim]"),
            Label(f"[dim]Confidence: {self._fp.confidence}[/dim]"),
            id="fp-box",
        )
        box.styles.offset = self._offset
        yield box

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:            # the transparent backdrop, not the box or its labels
            self.dismiss()


class ClientsList(Vertical):
    def __init__(self, clients, **kwargs) -> None:
        super().__init__(**kwargs)
        self._clients = clients
        # mac -> (pwr_label, pkts_label) for in-place updates without re-mounting.
        self._known: dict[str, tuple[Label, Label]] = {}
        # deauth button id -> client mac, so the screen's handler knows whom to
        # deauth from the inline ✕ that was clicked.
        self._by_button: dict[str, str] = {}
        # the fp/mac labels that pop up FingerprintDetail on click -> (mac, Fingerprint).
        # Only fingerprinted clients get an entry; a blank badge has nothing to show.
        self._detail_targets: dict[Label, tuple[str, Fingerprint]] = {}

    def compose(self) -> ComposeResult:
        # Broadcast button pinned at the top; only the row list scrolls below it.
        yield Button("Deauth all", id="deauth-all", classes="bcast-btn",
                     tooltip="Deauthenticate all clients (Broadcast)")
        yield VerticalScroll(
            *(self._make_row(c.bssid, c.power, c.packets, c.fingerprint_emoji,
                             c.fingerprint_label, c.fingerprint_confidence)
              for c in self._clients),
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
                # Skip if a same-id row is still mid-removal: it mounts cleanly
                # next tick once the removal lands, rather than duplicating the id.
                if self.query(f"#{_row_id(c.bssid)}"):
                    continue
                self._rows_host().mount(self._make_row(
                    c.bssid, c.power, c.packets, c.fingerprint_emoji,
                    c.fingerprint_label, c.fingerprint_confidence))
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
        for label, (target_mac, _fp) in list(self._detail_targets.items()):
            if target_mac == mac:
                del self._detail_targets[label]

    def client_mac(self, button_id: str) -> str | None:
        """The client MAC behind an inline-deauth ✕ button id (None if unknown)."""
        return self._by_button.get(button_id)

    def set_deauth_enabled(self, enabled: bool) -> None:
        """Enable/disable every deauth control at once: the broadcast button and
        each per-client ✕ (the only Buttons this list owns). Greyed when a
        PMF-Required AP would refuse the deauth, or another attack owns the radio."""
        disabled = not enabled
        for btn in self.query(Button):
            btn.disabled = disabled

    # 'Deauth all' is always visible (broadcast deauth is valid with no known
    # clients); set_deauth_enabled greys it when the AP refuses it (PMF-Required).

    # ----- fingerprint detail popup --------------------------------------------

    def on_click(self, event: events.Click) -> None:
        target = self._detail_targets.get(event.widget)
        if target is None:
            return
        mac, fp = target
        self.app.push_screen(FingerprintDetail(mac, fp, offset=(event.screen_x, event.screen_y)))

    # ----- helpers -----------------------------------------------------------

    def _make_row(self, mac: str, power: int, packets: int, fingerprint_emoji: str = "",
                  fingerprint_label: str = "", fingerprint_confidence: str = "high") -> Horizontal:
        pwr = Label(str(power), classes="cl-pwr")
        pkts = Label(str(packets), classes="cl-pkts")
        self._known[mac] = (pwr, pkts)
        btn_id = f"{_row_id(mac)}-deauth"
        self._by_button[btn_id] = mac
        fp_label = Label(fingerprint_emoji, classes="cl-fp")
        mac_label = Label(mac, classes="cl-bssid")
        if fingerprint_label:
            fp = Fingerprint(fingerprint_emoji, fingerprint_label, fingerprint_confidence)
            fp_label.tooltip = fingerprint_label
            fp_label.add_class("fp-known")
            mac_label.add_class("fp-known")
            self._detail_targets[fp_label] = (mac, fp)
            self._detail_targets[mac_label] = (mac, fp)
        return Horizontal(
            fp_label,
            mac_label,
            pwr, pkts,
            Button("✕", id=btn_id, classes="cl-deauth", tooltip="Deauthenticate Client"),
            classes="client-row", id=_row_id(mac),
        )

    def _update_title(self) -> None:
        self.border_title = f"CLIENTS ({len(self._known)})"
