"""Blocking-error modals: fatal (unrecoverable) and recoverable (adapter lost)."""
from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Label, Static

from wifit3.errors import WifiteDeviceLostError, WifiteFatalError


class FatalErrorModal(ModalScreen[None]):
    """An unrecoverable failure (e.g. no USB backend): red, Quit-only, with a copyable trace."""

    # No bindings: a fatal error is Quit-only, so Escape must not dismiss it.
    BINDINGS = []

    DEFAULT_CSS = """
    FatalErrorModal { align: center middle; }
    FatalErrorModal #dialog {
        width: 90; height: auto; max-width: 90%; max-height: 90%;
        border: thick $error; background: $surface; padding: 1 2;
    }
    FatalErrorModal #title {
        content-align: center middle; margin-bottom: 1; text-style: bold; color: $error;
    }
    FatalErrorModal #message { margin-bottom: 1; color: $error; }
    /* Collapsible and VerticalScroll both default to *filling* their parent, which balloons the
       dialog to its max-height and shoves the buttons off-screen. Pin both to content height; the
       trace then scrolls within max-height instead of growing the dialog. */
    FatalErrorModal Collapsible { height: auto; }
    FatalErrorModal #trace-scroll { height: auto; max-height: 10; }
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
                with VerticalScroll(id="trace-scroll"):
                    yield Static(self._error.trace, id="trace")
            with Horizontal(id="button-row"):
                yield Button("Copy details", variant="default", id="btn-copy")
                yield Button("Quit", variant="error", id="btn-quit")

    @on(Button.Pressed, "#btn-copy")
    def _copy(self) -> None:
        # OSC-52 copy — best-effort (not every terminal supports it), so the trace also stays
        # visible/selectable in the Details box as the manual-copy fallback.
        self.app.copy_to_clipboard(self._error.trace)
        self.notify("Details copied to clipboard.")

    @on(Button.Pressed, "#btn-quit")
    def _quit(self) -> None:
        self.app.exit()


class RecoverableErrorModal(ModalScreen[None]):
    """A recoverable failure (adapter lost mid-run): orange, offers Back to Splash."""

    # No bindings: force an explicit choice (Escape must not dismiss).
    BINDINGS = []

    DEFAULT_CSS = """
    RecoverableErrorModal { align: center middle; }
    RecoverableErrorModal #dialog {
        width: 90; height: auto; max-width: 90%; max-height: 90%;
        border: thick $warning; background: $surface; padding: 1 2;
    }
    RecoverableErrorModal #title {
        content-align: center middle; margin-bottom: 1; text-style: bold; color: $text-warning;
    }
    RecoverableErrorModal #message { margin-bottom: 1; color: $text-warning; }
    RecoverableErrorModal #button-row { height: auto; align: center middle; margin-top: 1; }
    RecoverableErrorModal #button-row Button { margin: 0 1; }
    """

    def __init__(self, error: WifiteDeviceLostError) -> None:
        super().__init__()
        self._error = error

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._error.title, id="title")
            yield Static(self._error.message, id="message")
            with Horizontal(id="button-row"):
                yield Button("Back to Splash", variant="warning", id="btn-splash")

    @on(Button.Pressed, "#btn-splash")
    def _back_to_splash(self) -> None:
        self.app.run_worker(self.app.recover_to_splash(), exclusive=True)
