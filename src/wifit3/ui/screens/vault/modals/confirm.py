from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class ConfirmModal(ModalScreen[bool]):
    """A yes/no question; No has focus, so a stray Enter does nothing."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    ConfirmModal #dialog { width: 54; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    ConfirmModal #prompt { margin-bottom: 1; }
    ConfirmModal Horizontal { align: right middle; height: auto; }
    ConfirmModal Button { margin-left: 1; }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text.from_markup(self._prompt, emoji=False), id="prompt")
            with Horizontal():
                yield Button("No", "default", id="no")
                yield Button("Yes", "error", id="yes")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    @on(Button.Pressed, "#yes")
    def _yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self) -> None:
        self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)
