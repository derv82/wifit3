"""Wait-for-user-to-replug modal (Linux)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, LoadingIndicator

_UNPLUG = ("Uprawnienia dla [bold]{name}[/] są zainstalowane.\n"
           "Trzeba ponownie wyjąć i włożyć kartę, by odświeżyć jej stan.\n\n"
           "Oczekiwanie, aż [bold $text-warning]wyjmiesz kartę…[/]")
_REPLUG = ("[bold $text-success]Karta wyjęta ✓[/]\n\n"
           "Oczekiwanie, aż [bold $text-warning]włożysz ją z powrotem…[/]")


class ReplugModal(ModalScreen[str]):
    """Watches for an unplug→replug via the injected presence-waiter (no device-manager handle, so
    it never drives a bus rescan itself). Dismisses "replugged" / "skip" / "timeout"."""

    BINDINGS = [Binding("escape", "skip", "Pomiń", show=True)]

    DEFAULT_CSS = """
    ReplugModal { align: center middle; }
    ReplugModal #dialog {
        width: 64; max-width: 90%; height: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    ReplugModal #title { width: 1fr; text-align: center; text-style: bold; margin-bottom: 1; }
    ReplugModal #status { width: 1fr; text-align: center; margin-bottom: 1; }
    ReplugModal #spin { height: 1; margin-bottom: 1; }
    ReplugModal #button-row { height: auto; align: center middle; }
    ReplugModal #btn-skip { background: $primary; color: auto; }
    """

    def __init__(self, name: str, wait_present) -> None:
        super().__init__()
        self._name = name
        self._wait_present = wait_present   # async (present: bool) -> bool
        self._done = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[bold $text-warning]Ponowne podłączenie[/]", id="title")
            yield Label(_UNPLUG.format(name=self._name), id="status")
            with Center(id="spin"):
                yield LoadingIndicator()
            with Horizontal(id="button-row"):
                yield Button("Pomiń", id="btn-skip")

    def on_mount(self) -> None:
        self.run_worker(self._watch(), exclusive=True)

    async def _watch(self) -> None:
        gone = await self._wait_present(False)
        if self._done:
            return
        if not gone:
            self._finish("timeout")
            return
        self.query_one("#status", Label).update(_REPLUG)
        back = await self._wait_present(True)
        if self._done:
            return
        self._finish("replugged" if back else "timeout")

    def _finish(self, result: str) -> None:
        if self._done:
            return
        self._done = True
        self.dismiss(result)          # removing the screen cancels the still-waiting worker

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._finish("skip")

    def action_skip(self) -> None:
        self._finish("skip")
