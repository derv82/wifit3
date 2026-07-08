"""Active replug gate (Linux).

After a Linux install of a replug-required chipset, the card is still warm from the kernel driver
and can't cold-reset in userland — only a physical power-cycle recovers RX. This modal watches the
USB bus for the unplug, then the replug (a fresh, cold enumeration), and dismisses ``"replugged"``
so the splash can auto-connect the now-cold card. The Skip button (and Escape) dismiss ``"skip"``
— the user will replug and press START themselves; a per-phase timeout dismisses ``"timeout"``.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, LoadingIndicator

_UNPLUG = ("Permissions are installed for the [bold]{name}[/].\n"
           "A replug is required to refresh the card's state.\n\n"
           "Waiting for you to [bold $text-warning]unplug the card now…[/]")
_REPLUG = ("[bold $text-success]Card removed ✓[/]\n\n"
           "Waiting for you to [bold $text-warning]plug it back in…[/]")


class ReplugModal(ModalScreen[str]):
    """Watches for unplug→replug of ``vid:pid``. Dismisses "replugged" / "skip" / "timeout"."""

    BINDINGS = [Binding("escape", "skip", "Skip", show=True)]

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

    def __init__(self, device_manager, vid: int, pid: int, name: str) -> None:
        super().__init__()
        self._dm = device_manager
        self._vid = vid
        self._pid = pid
        self._name = name
        self._done = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[bold $text-warning]Replug Required[/]", id="title")
            yield Label(_UNPLUG.format(name=self._name), id="status")
            with Center(id="spin"):
                yield LoadingIndicator()
            with Horizontal(id="button-row"):
                yield Button("Skip", id="btn-skip")

    def on_mount(self) -> None:
        self.run_worker(self._watch(), exclusive=True)

    async def _watch(self) -> None:
        gone = await self._dm.linux_wait_for_presence(self._vid, self._pid, present=False)
        if self._done:
            return
        if not gone:
            self._finish("timeout")
            return
        self.query_one("#status", Label).update(_REPLUG)
        back = await self._dm.linux_wait_for_presence(self._vid, self._pid, present=True)
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
