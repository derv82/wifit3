"""Modal spinner shown while a privileged udev grant/revoke propagates to the live device."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, LoadingIndicator


class PropagatingDialog(ModalScreen[None]):
    DEFAULT_CSS = """
    PropagatingDialog { align: center middle; }
    PropagatingDialog #dialog {
        width: 50; height: auto; max-width: 90%;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    PropagatingDialog #message {
        content-align: center middle; margin-bottom: 1; text-style: bold;
    }
    PropagatingDialog LoadingIndicator { height: 1; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message, id="message")
            yield LoadingIndicator()
