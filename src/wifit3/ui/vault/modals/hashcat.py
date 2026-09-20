from typing import Dict, Any
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, Button


class HashcatConfigModal(ModalScreen[Dict[str, Any]]):
    """Placeholder modal for configuring Hashcat arguments."""
    
    DEFAULT_CSS = """
    HashcatConfigModal {
        align: center middle;
    }
    HashcatConfigModal > Vertical {
        width: 40;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("[bold]Configure Hashcat[/bold]\n\n(Placeholder UI: Add wordlist pickers and config args here later!)")
            yield Button("Launch Placeholder Job", id="launch", variant="success")
            yield Button("Cancel", id="cancel", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "launch":
            self.dismiss({"placeholder_args": True})
        elif event.button.id == "cancel":
            self.dismiss()
