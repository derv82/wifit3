from typing import Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from wifit3.persist.config import Config


class NicknameModal(ModalScreen[bool]):
    """Edit an AP's nickname in Config ("" clears it); dismisses with whether it saved."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    NicknameModal { align: center middle; }
    NicknameModal #dialog {
        width: 58; height: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    NicknameModal #title { width: 100%; text-align: center; text-style: bold; }
    NicknameModal #network { color: $text-muted; margin-bottom: 1; }
    NicknameModal #buttons { height: auto; align: right middle; margin-top: 1; }
    NicknameModal #buttons Button { margin-left: 1; }
    """

    def __init__(self, ssid: Optional[str], bssid: str) -> None:
        super().__init__()
        self._ssid = ssid
        self._bssid = bssid

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Set Router Nickname", id="title")
            yield Label(self._ssid or "Hidden network", markup=False, id="network")
            yield Input(Config.nickname(self._bssid) or "", placeholder="Nickname (leave empty to clear)", id="nickname")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    def on_mount(self) -> None:
        field = self.query_one("#nickname", Input)
        field.focus()
        field.action_end()

    @on(Input.Submitted, "#nickname")
    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        nickname = self.query_one("#nickname", Input).value.strip()
        Config.set_nickname(self._bssid, nickname)
        self.app.persist_config()
        self.notify("Nickname saved" if nickname else "Nickname cleared")
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(False)
