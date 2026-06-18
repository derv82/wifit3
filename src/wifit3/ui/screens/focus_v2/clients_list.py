"""Clients list — bottom-right, bordered. Broadcast deauth pinned at the top,
then one compact left-aligned row per client: BSSID · power · packets · an
inline ``✕`` (white-on-red) that deauths just that client — no select-then-act.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label


class ClientsList(Vertical):
    def __init__(self, clients, **kwargs) -> None:
        super().__init__(**kwargs)
        self._clients = clients

    def compose(self) -> ComposeResult:
        yield Button("Deauth all", id="deauth-all", classes="bcast-btn")
        for i, c in enumerate(self._clients):
            with Horizontal(classes="client-row"):
                yield Label(c.bssid, classes="cl-bssid")
                yield Label(f"{c.power}", classes="cl-pwr")
                yield Label(f"{c.packets}", classes="cl-pkts")
                yield Button("✕", id=f"cl-deauth-{i}", classes="cl-deauth")

    def on_mount(self) -> None:
        self.border_title = f"CLIENTS ({len(self._clients)})"
