"""The unified bring-up progress modal: one status surface for connect() and for install, shown from
any screen. Confirm / replug / error dialogs stack on top of it and pop back to it; it is never
rewritten into another modal."""
from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, ProgressBar

# The status line is truncated to the dialog's inner width so a long driver/wdi message can't wrap
# and jitter the modal height (a period fix is shorter messages at the source).
_STATUS_MAX = 58


class BringupProgressModal(ModalScreen):
    """A title, a status line, and a progress bar. Driven by BringupPrompter via set_status /
    set_progress; carries no logic of its own."""

    DEFAULT_CSS = """
    BringupProgressModal { align: center middle; layers: dialog wiffy; }
    BringupProgressModal #dialog {
        layer: dialog;
        width: 64; max-width: 90%; height: auto;
        border: thick $success; background: $surface; padding: 1 2;
    }
    BringupProgressModal #title { width: 1fr; text-align: center; text-style: bold; margin-bottom: 1; }
    BringupProgressModal #status { width: 1fr; text-align: center; margin-bottom: 1; }
    BringupProgressModal #bar { width: 100%; }
    BringupProgressModal #bar Bar { width: 1fr; }
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self._title = title
        self._status = "Starting…"
        self._wiffy = None

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="title")
            yield Label(self._status, id="status")
            yield ProgressBar(total=100, show_eta=False, id="bar")

    def set_status(self, message: str) -> None:
        if len(message) > _STATUS_MAX:
            message = message[:_STATUS_MAX - 1] + "…"
        self._status = message
        if self.is_mounted:
            self.query_one("#status", Label).update(message)

    def set_progress(self, fraction: float) -> None:
        if self.is_mounted:
            self.query_one(ProgressBar).progress = max(0.0, min(1.0, fraction)) * 100

    async def show_assistant(self, greeting: str, messages: list[str],
                             *, intro_delay: float = 2.0) -> None:
        """Mount WiFFy and kick off his slide-in. Fire-and-forget: enter() self-paces the intro
        delay + animation while the elevated op runs; hide_assistant() reverses it."""
        from wifit3.ui.wiffy import WiffyAssistant
        self._wiffy = WiffyAssistant(greeting, list(messages))
        await self.mount(self._wiffy)
        self._wiffy.run_worker(self._wiffy.enter(intro_delay=intro_delay), name="wiffy-enter")

    async def hide_assistant(self, ok: bool) -> None:
        if self._wiffy is not None:
            await self._wiffy.exit(ok=ok)
            self._wiffy = None

    def on_resize(self, event: events.Resize) -> None:
        if self._wiffy is not None:
            self._wiffy.reposition()
