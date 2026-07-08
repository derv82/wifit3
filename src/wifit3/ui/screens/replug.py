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

_UNPLUG = ("[bold]{name}[/] is set up, but it's still warm from the kernel driver.\n\n"
           "[bold $text-warning]Unplug the card now[/] — I'll detect it and continue automatically.")
_REPLUG = ("[bold green]Card removed ✓[/]\n\n"
           "[bold $text-warning]Now plug it back in[/] — it comes up fresh and connects on its own.")


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
            yield Label("One more step — replug the card", id="title")
            yield Label(_UNPLUG.format(name=self._name), id="status")
            with Center(id="spin"):
                yield LoadingIndicator()
            with Horizontal(id="button-row"):
                yield Button("Skip (I'll replug + press START)", variant="default", id="btn-skip")

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
