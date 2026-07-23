"""The unified bring-up progress modal: one status surface for connect() and for install, shown from
any screen. Confirm / replug / error dialogs stack on top of it and pop back to it; it is never
rewritten into another modal."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Center, Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, ProgressBar


class BringupProgressModal(ModalScreen):
    """A title, a status line, and a progress bar. Driven by BringupPrompter via set_status /
    set_progress; carries no logic of its own."""

    DEFAULT_CSS = """
    BringupProgressModal { align: center middle; }
    BringupProgressModal #dialog {
        width: 64; max-width: 90%; height: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    BringupProgressModal #title { width: 1fr; text-align: center; text-style: bold; margin-bottom: 1; }
    BringupProgressModal #status { width: 1fr; text-align: center; margin-bottom: 1; }
    BringupProgressModal #bar { height: auto; align: center middle; }
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title
        self._status = "Starting…"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Label(self._status, id="status")
            with Center(id="bar"):
                yield ProgressBar(total=100, show_eta=False)

    def set_status(self, message: str) -> None:
        self._status = message
        if self.is_mounted:
            self.query_one("#status", Label).update(message)

    def set_progress(self, fraction: float) -> None:
        if self.is_mounted:
            self.query_one(ProgressBar).progress = max(0.0, min(1.0, fraction)) * 100
