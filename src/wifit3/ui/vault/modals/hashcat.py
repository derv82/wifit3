from textual.app import ComposeResult
from textual.containers import Vertical, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label
import shutil
import sys


def _default_hashcat_path() -> str:
    if found := shutil.which("hashcat"):
        return found
    if sys.platform == "win32":
        return r"C:\hashcat\hashcat.exe"
    if sys.platform == "darwin":
        return "/opt/homebrew/bin/hashcat"
    return "/usr/bin/hashcat"


class HashcatConfigModal(ModalScreen[dict]):
    """Configuration modal for Hashcat jobs."""

    DEFAULT_CSS = """
    HashcatConfigModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.5);
    }
    #hashcat-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: solid $primary;
    }
    .hashcat-label {
        margin-top: 1;
    }
    #hashcat-buttons {
        margin-top: 2;
        align: right middle;
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="hashcat-dialog"):
            yield Label("Launch Hashcat", classes="text-bold")
            
            yield Label("Hashcat Executable Path:", classes="hashcat-label")
            yield Input(value=_default_hashcat_path(), id="hashcat-exe")
            
            yield Label("Wordlist Path:", classes="hashcat-label")
            yield Input(placeholder="e.g. D:\\wordlists\\Top29Million.txt", id="hashcat-wordlist")
            
            with Horizontal(id="hashcat-buttons"):
                yield Button("Cancel", variant="error", id="hashcat-cancel")
                yield Button("Launch", variant="success", id="hashcat-launch")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "hashcat-cancel":
            self.dismiss(None)
        elif event.button.id == "hashcat-launch":
            exe = self.query_one("#hashcat-exe", Input).value.strip()
            wordlist = self.query_one("#hashcat-wordlist", Input).value.strip()
            if not exe or not wordlist:
                self.notify("Please provide both executable and wordlist paths", severity="error")
                return
            self.dismiss({
                "hashcat_exe": exe,
                "wordlist": wordlist
            })
