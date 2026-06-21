"""Active-Monitor capability warning, shown before a WPS-PIN brute-force on a card that can't
HW-ACK a spoofed MAC.

The PIN sweep is up to ~11,000 separate ACKed conversations; without active-monitor each one
relies on the AP tolerating an un-ACKed exchange, which frequently times out. So unlike the
one-shot PBC path (which just logs a warning and proceeds), PIN gates behind this modal.
``hardware`` distinguishes the silicon genuinely lacking it (NONE) from this driver not
porting it (UNIMPLEMENTED). Dismisses ``True`` (continue anyway) / ``False`` (stop).
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label


class ActiveMonitorWarningDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("c", "go", "Continue", show=True),
        Binding("s", "stop", "Stop", show=True),
        Binding("escape", "stop", "Stop", show=True),
    ]

    DEFAULT_CSS = """
    ActiveMonitorWarningDialog { align: center middle; }
    ActiveMonitorWarningDialog #dialog {
        width: auto; max-width: 80%; height: auto;
        border: thick $warning; background: $surface; padding: 1 2;
    }
    ActiveMonitorWarningDialog #headline {
        width: 1fr; text-align: center; margin-bottom: 1; text-style: bold;
    }
    ActiveMonitorWarningDialog #body { width: 1fr; text-align: center; margin-bottom: 1; }
    ActiveMonitorWarningDialog #button-row { height: auto; align: center middle; }
    ActiveMonitorWarningDialog #button-row Button { margin: 0 2; }
    """

    def __init__(self, hardware: bool) -> None:
        super().__init__()
        self._hardware = hardware

    def compose(self) -> ComposeResult:
        headline = ("This card cannot do Active Monitor (hardware limitation)."
                    if self._hardware else
                    "The driver for this card does not support Active Monitor.")
        with Vertical(id="dialog"):
            yield Label(f"[orange1]{headline}[/orange1]", id="headline")
            yield Label(
                "WPS PIN brute-force requires up to ~11,000 ACKed exchanges.\n"
                "Without Active Monitor, PIN attempts frequently timeout and fail.",
                id="body")
            with Horizontal(id="button-row"):
                yield Button("Continue anyway", variant="warning", id="btn-go")
                yield Button("Stop", variant="default", id="btn-stop")

    def on_mount(self) -> None:
        self.query_one("#btn-stop", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-go")

    def action_go(self) -> None:
        self.dismiss(True)

    def action_stop(self) -> None:
        self.dismiss(False)
