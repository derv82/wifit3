from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class ImportProblemsModal(ModalScreen[None]):
    """An import's counts, then one ``file - problem`` line per file or line it skipped."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]
    CSS = """
    ImportProblemsModal { align: center middle; }
    ImportProblemsModal #dialog {
        width: 100; max-width: 95%; height: auto; max-height: 80%;
        border: thick $warning; background: $surface; padding: 1 2;
    }
    ImportProblemsModal #title { text-style: bold; margin-bottom: 1; }
    ImportProblemsModal #summary { margin-bottom: 1; }
    ImportProblemsModal VerticalScroll { height: auto; max-height: 20; }
    ImportProblemsModal Horizontal { align: right middle; height: auto; margin-top: 1; }
    ImportProblemsModal Button { margin-right: 0; }
    """

    def __init__(self, summary: str, problems: list[tuple[str, str]]) -> None:
        super().__init__()
        self._summary = summary
        self._problems = problems

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Vault: Import results", id="title")
            yield Label(self._summary, id="summary", markup=False)
            with VerticalScroll():
                yield Static("\n".join(f"{name} - {problem}" for name, problem in self._problems), markup=False)
            with Horizontal():
                yield Button("Close", "primary", id="close")

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss()
