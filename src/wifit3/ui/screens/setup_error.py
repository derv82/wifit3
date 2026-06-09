"""Modal shown when a device-setup action fails (WinUSB install/restore). [DEVICE-SETUP.md]

Renders a title, a human message, and an optional Details line (the raw libwdi / Win32
code) — the §2c hardware-failure UX. Dismisses with ``None``; the caller just awaits it
for acknowledgement.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class SetupErrorDialog(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close", show=True),
        Binding("enter", "close", "Close", show=True, priority=True),
    ]

    DEFAULT_CSS = """
    SetupErrorDialog { align: center middle; }
    SetupErrorDialog #dialog {
        width: 60; height: auto; max-width: 90%;
        border: thick $error; background: $surface; padding: 1 2;
    }
    SetupErrorDialog #title {
        content-align: center middle; margin-bottom: 1; text-style: bold; color: $error;
    }
    SetupErrorDialog #message { margin-bottom: 1; }
    SetupErrorDialog #details { color: $text-muted; margin-bottom: 1; }
    SetupErrorDialog #button-row { height: auto; align: center middle; }
    """

    def __init__(self, title: str, message: str, details: str | None = None) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._details = details

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Label(self._message, id="message")
            if self._details:
                yield Label(self._details, id="details")
            with Horizontal(id="button-row"):
                yield Button("Close", variant="primary", id="btn-close")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)
