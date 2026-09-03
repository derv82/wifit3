from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class ConfirmScannerExitDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "Leave", show=True),
        Binding("n", "no", "Stay", show=True),
        Binding("escape", "no", "Stay", show=True),
    ]

    DEFAULT_CSS = """
    ConfirmScannerExitDialog { align: center middle; }
    ConfirmScannerExitDialog #dialog {
        width: 60; max-width: 90%; height: auto;
        border: thick $warning; background: $surface; padding: 1 2;
    }
    ConfirmScannerExitDialog #title {
        width: 1fr; text-align: center; margin-bottom: 1; text-style: bold; color: $text-warning;
    }
    ConfirmScannerExitDialog #body { width: 1fr; text-align: center; margin-bottom: 1; }
    ConfirmScannerExitDialog #button-row { height: auto; align: center middle; }
    ConfirmScannerExitDialog #button-row Button { margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Leave scanner?", id="title")
            yield Label(
                "This will stop scanning, close the active adapter, and return to Splash.", id="body")
            with Horizontal(id="button-row"):
                yield Button("Leave", variant="warning", id="btn-yes")
                yield Button("Stay", variant="default", id="btn-no")

    def on_mount(self) -> None:
        self.query_one("#btn-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)
