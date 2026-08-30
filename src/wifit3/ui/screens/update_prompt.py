from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class UpdatePromptDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "Update", show=True),
        Binding("n", "no", "Not now", show=True),
        Binding("escape", "no", "Not now", show=True),
    ]

    DEFAULT_CSS = """
    UpdatePromptDialog { align: center middle; }
    UpdatePromptDialog #dialog {
        width: 58; max-width: 90%; height: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    UpdatePromptDialog #title {
        width: 1fr; text-align: center; margin-bottom: 1; text-style: bold;
    }
    UpdatePromptDialog #body { width: 1fr; text-align: center; margin-bottom: 1; }
    UpdatePromptDialog #button-row { height: auto; align: center middle; }
    UpdatePromptDialog #button-row Button { margin: 0 1; }
    """

    def __init__(self, version: str, *, title: str = "Update Wifit3?", body: str | None = None,
                 yes_label: str = "Update", no_label: str = "Not now") -> None:
        super().__init__()
        self._title = title
        self._body = body or f"Version [bold]{version}[/] is available. Update now and restart?"
        self._yes_label = yes_label
        self._no_label = no_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Label(self._body, id="body")
            with Horizontal(id="button-row"):
                yield Button(self._yes_label, variant="success", id="btn-yes")
                yield Button(self._no_label, variant="default", id="btn-no")

    def on_mount(self) -> None:
        self.query_one("#btn-no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)
