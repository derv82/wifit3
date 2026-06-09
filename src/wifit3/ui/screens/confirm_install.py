"""WinUSB install confirmation — a visual-guide modal. [DEVICE-SETUP.md]

Shown when the user STARTs a supported card that isn't WinUSB-bound yet. It draws the
Wifit3 ↔ WinUSB ↔ card chain with the WinUSB link flagged as the missing REQUIRED piece
(a pulsing red badge), explains the reversible driver swap, and asks to install. Dismisses
with ``True`` (install) / ``False`` (cancel). Copy is kept plain for non-native English
readers.
"""
from __future__ import annotations

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

# Red shades cycled to make the REQUIRED badge pulse (ping-pong for a smooth throb).
_PULSE = ["#6e0000", "#960000", "#c00000", "#ff2a2a", "#c00000", "#960000"]


class ConfirmInstallDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "Install", show=True),
        Binding("n", "no", "Cancel", show=True),
        Binding("escape", "no", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    ConfirmInstallDialog { align: center middle; }
    ConfirmInstallDialog #dialog {
        width: auto; max-width: 96%; height: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    ConfirmInstallDialog #title {
        width: 1fr; text-align: center; margin-bottom: 1; text-style: bold;
    }
    ConfirmInstallDialog #diagram { content-align: center middle; margin-bottom: 1; }
    ConfirmInstallDialog #warn { width: 1fr; text-align: center; margin-bottom: 1; }
    ConfirmInstallDialog #question {
        width: 1fr; text-align: center; margin-bottom: 1; text-style: bold;
    }
    ConfirmInstallDialog #button-row { height: auto; align: center middle; }
    ConfirmInstallDialog #button-row Button { margin: 0 2; }
    """

    def __init__(self, description: str) -> None:
        super().__init__()
        self._full = description
        # Short name for the diagram box (the chipset half of "Chipset / Adapter", capped so
        # a long name doesn't blow out the box width).
        self._short = description.split(" / ")[0][:18]
        self._pulse_i = 0

    def _diagram(self, pulse: str) -> Table:
        """The Wifit3 ◄──► WinUSB ◄──► card chain + status row, as a Rich grid (Rich handles
        the column alignment so the ✓/✗ land under their boxes). ``pulse`` is the REQUIRED
        badge's current background red."""
        grid = Table.grid(padding=(0, 1))
        for _ in range(5):
            grid.add_column(justify="center", vertical="middle")
        grid.add_row(
            Panel("Wifit3", border_style="green", expand=False, padding=(0, 1)),
            Text("◄──►", style="bold"),
            Panel("WinUSB Driver", border_style="red", expand=False, padding=(0, 1)),
            Text("◄──►", style="bold"),
            Panel(self._short, border_style="green", expand=False, padding=(0, 1)),
        )
        grid.add_row(
            Text("✓", style="bold green"),
            Text(""),
            Text(" ✗ REQUIRED ", style=f"bold white on {pulse}"),
            Text(""),
            Text("✓ SUPPORTED", style="bold green"),
        )
        return grid

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Wifit3 needs the WinUSB driver to talk to this card", id="title")
            yield Static(self._diagram(_PULSE[0]), id="diagram")
            yield Label(
                "[bold $text-warning]Warning:[/] this [italic $text-warning]replaces the "
                "card's current driver[/] — Windows will stop seeing it as a wireless "
                "device.\n[dim]Reversible: uninstall the WinUSB driver in Device Manager to "
                "restore it.[/dim]",
                id="warn")
            yield Label(f"Install WinUSB for [bold]{self._full}[/]?", id="question")
            with Horizontal(id="button-row"):
                yield Button("Install", variant="success", id="btn-yes")
                yield Button("Cancel", variant="default", id="btn-no")

    def on_mount(self) -> None:
        self.query_one("#btn-yes", Button).focus()
        # Pulse the REQUIRED badge. The diagram is a small grid; re-rendering it per tick is
        # cheap, and it's only alive while the modal is open.
        self.set_interval(0.16, self._pulse)

    def _pulse(self) -> None:
        self._pulse_i = (self._pulse_i + 1) % len(_PULSE)
        self.query_one("#diagram", Static).update(self._diagram(_PULSE[self._pulse_i]))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)
