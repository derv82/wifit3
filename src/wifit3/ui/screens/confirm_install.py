"""Yes/No modal asking whether to install WinUSB for a card the user just tried to start.

Shown when ``connect()`` fails because the card isn't WinUSB-bound yet. Dismisses with
``True`` (install) or ``False`` (don't). Copy is kept plain for non-native English readers.
[DEVICE-SETUP.md]
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class ConfirmInstallDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "Yes", show=True),
        Binding("n", "no", "No", show=True),
        Binding("escape", "no", "No", show=True),
    ]

    DEFAULT_CSS = """
    ConfirmInstallDialog { align: center middle; }
    ConfirmInstallDialog #dialog {
        width: 62; height: auto; max-width: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    ConfirmInstallDialog #title {
        content-align: center middle; margin-bottom: 1; text-style: bold;
    }
    ConfirmInstallDialog #message { margin-bottom: 1; }
    ConfirmInstallDialog #button-row { height: auto; align: center middle; }
    """

    def __init__(self, description: str) -> None:
        super().__init__()
        self._description = description

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Install the WinUSB driver?", id="title")
            yield Label(
                f"{self._description} needs the WinUSB driver before Wifit3 can use it. "
                "This changes the card's driver and can be undone later.",
                id="message")
            with Horizontal(id="button-row"):
                yield Button("Yes, install", variant="success", id="btn-yes")
                yield Button("No", variant="default", id="btn-no")

    def on_mount(self) -> None:
        self.query_one("#btn-yes", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)
