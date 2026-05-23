"""Modal dialog for the Shift+D 'decloak test' action — prompts the user
for an explicit SSID (or comma-separated list of SSIDs) to probe a
selected AP with. Used to verify the TX/RX pipeline against a router
the user has deliberately configured (e.g. an old AP set to a known
hidden SSID), bypassing the sibling-based candidate generator entirely.
"""
from __future__ import annotations

from typing import List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class DecloakSsidDialog(ModalScreen[Optional[List[str]]]):
    """Single-line SSID input. Returns a list[str] (split on commas) on
    submit, or None on cancel / empty input."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("enter", "confirm", "Send", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    DecloakSsidDialog {
        align: center middle;
    }
    DecloakSsidDialog #dialog {
        width: 64;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    DecloakSsidDialog #title {
        content-align: center middle;
        margin-bottom: 1;
        text-style: bold;
    }
    DecloakSsidDialog #hint {
        color: $text-muted;
        content-align: center middle;
        margin-bottom: 1;
    }
    DecloakSsidDialog Input {
        margin-bottom: 1;
    }
    DecloakSsidDialog #button-row {
        height: auto;
        align: center middle;
    }
    """

    def __init__(self, bssid: str, prefill: str = "") -> None:
        super().__init__()
        self._bssid = bssid
        self._prefill = prefill

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"Decloak test against {self._bssid}", id="title")
            yield Label(
                "Enter SSID(s) to probe with — comma-separated for multiple",
                id="hint",
            )
            yield Input(value=self._prefill, placeholder="e.g. MyTestSSID", id="ssid-input")
            with Horizontal(id="button-row"):
                yield Button("Send", variant="primary", id="btn-send")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#ssid-input", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_confirm(self) -> None:
        text = self.query_one("#ssid-input", Input).value.strip()
        if not text:
            self.dismiss(None)
            return
        ssids = [s.strip() for s in text.split(",") if s.strip()]
        self.dismiss(ssids if ssids else None)

    def on_input_submitted(self, _: Input.Submitted) -> None:
        # Enter inside the Input widget should also submit the dialog.
        self.action_confirm()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-send":
            self.action_confirm()
        else:
            self.action_cancel()
