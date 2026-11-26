from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from wifit3.persist.capture_import import CaptureImport


def import_summary(result: CaptureImport) -> str:
    """``Imported N files:``, then one right-aligned line per non-zero count."""
    rows = [(len(result.bssids), "Access Point", "Access Points", "$text-success"),
            (result.handshakes, "Handshake", "Handshakes", "$text-success"),
            (result.pmkids, "PMKID", "PMKIDs", "$text-success"),
            (result.keys, "Key", "Keys", "$text-success"),
            (result.duplicates, "Already in Vault", "Already in Vault", ""),
            (len(result.problems), "Error", "Errors", "$text-error")]
    width = len(str(max(count for count, *_ in rows)))
    lines = [f"[bold]Imported {result.files} {'file' if result.files == 1 else 'files'}:[/]"]
    for count, singular, plural, color in rows:
        if count == 0:
            continue
        text = f"{count:>{width}} {singular if count == 1 else plural}"
        lines.append(f"  [{color}]{text}[/]" if color else f"  {text}")
    return "\n".join(lines)


class ImportResultsModal(ModalScreen[None]):
    """An import's counts, then one ``file - problem`` line per file or line it skipped."""

    BINDINGS = [Binding("escape", "dismiss", "Close")]
    CSS = """
    ImportResultsModal { align: center middle; }
    ImportResultsModal #dialog {
        width: 100; max-width: 95%; height: auto; max-height: 80%;
        border: thick $warning; background: $surface; padding: 1 2;
    }
    ImportResultsModal #title { width: 100%; text-align: center; text-style: bold; color: $accent; margin-bottom: 1; }
    ImportResultsModal #summary { width: auto; margin-bottom: 1; }
    ImportResultsModal VerticalScroll { height: auto; max-height: 20; border-top: solid $panel-lighten-2; }
    ImportResultsModal #problems { color: $text-error; }
    ImportResultsModal Horizontal { align: center middle; height: auto; margin-top: 1; }
    ImportResultsModal Button { margin-right: 0; }
    """

    def __init__(self, result: CaptureImport) -> None:
        super().__init__()
        self._result = result

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Vault: Import results", id="title")
            with Center():
                yield Static(import_summary(self._result), id="summary")
            with VerticalScroll():
                yield Static("\n".join(f"{name} - {problem}" for name, problem in self._result.problems),
                             id="problems", markup=False)
            with Horizontal():
                yield Button("OK", "primary", id="ok")

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.dismiss()
