from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label


class NicknameModal(ModalScreen[str | None]):
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

    def __init__(self, ssid: str, nickname: str = "") -> None:
        super().__init__()
        self._ssid = ssid
        self._nickname = nickname

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Set Router Nickname", id="title")
            yield Label(self._ssid or "Hidden network", id="network")
            yield Input(self._nickname, placeholder="Nickname (leave empty to clear)", id="nickname")
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
        self.dismiss(self.query_one("#nickname", Input).value.strip())

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    def action_cancel(self) -> None:
        self.dismiss(None)
