"""Modal for an unrecoverable condition (e.g. no USB backend).

Unlike the dismissable :class:`SetupErrorDialog`, a fatal error has no recovery: the app can't
proceed, so this modal is Quit-only and Escape cannot close it. It shows the actionable message
plus a collapsible, plain-text trace the user can copy into a bug report.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Label, Static

from wifit3.errors import WifiteFatalError


class FatalErrorModal(ModalScreen[None]):
    # No bindings on purpose: a fatal error is Quit-only, so Escape must not dismiss it.
    # ModalScreen doesn't bind escape by default (SetupErrorDialog adds it explicitly) — we don't.
    BINDINGS = []

    DEFAULT_CSS = """
    FatalErrorModal { align: center middle; }
    FatalErrorModal #dialog {
        width: 72; height: auto; max-width: 90%; max-height: 90%;
        border: thick $error; background: $surface; padding: 1 2;
    }
    FatalErrorModal #title {
        content-align: center middle; margin-bottom: 1; text-style: bold; color: $error;
    }
    FatalErrorModal #message { margin-bottom: 1; color: $error; }
    FatalErrorModal #trace { color: $text-muted; }
    FatalErrorModal #button-row { height: auto; align: center middle; margin-top: 1; }
    FatalErrorModal #button-row Button { margin: 0 1; }
    """

    def __init__(self, error: WifiteFatalError) -> None:
        super().__init__()
        self._error = error

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._error.title, id="title")
            yield Static(self._error.message, id="message")
            with Collapsible(title="Details", collapsed=True):
                yield Static(self._error.trace, id="trace")
            with Horizontal(id="button-row"):
                yield Button("Copy details", variant="default", id="btn-copy")
                yield Button("Quit", variant="error", id="btn-quit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-copy":
            # OSC-52 copy — best-effort (not every terminal supports it), so the trace also stays
            # visible/selectable in the Details box as the manual-copy fallback.
            self.app.copy_to_clipboard(self._error.trace)
            self.notify("Details copied to clipboard.")
        else:
            self.app.exit()
