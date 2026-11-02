from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Optional

from rich.markup import escape
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from wifit3.models import PersistedCapture
from wifit3.persist.config import Config, ConfigError
from wifit3.persist.potfile import PotfileEntry
from wifit3.vault.tools.dwpa_api import (DwpaError, is_valid_key, network_leaked, retrieve_cracked,
                                         upload_capture)

_KEY_HINT = "[dim]32 lowercase hex characters, as issued by the DWPA server[/dim]"
_LEAKED = "[$warning]⚠ Possibly already cracked on DWPA[/]"
_NOT_LEAKED = "[dim]Not found on DWPA[/]"


class DwpaConfigModal(ModalScreen[bool]):
    """Server URL and key for uploads; dismisses True once the capture is accepted, else None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    DwpaConfigModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.5);
    }
    #dwpa-dialog {
        width: 60;
        max-width: 100%;
        height: auto;
        max-height: 100%;
        padding: 1 2 0 2;
        background: $surface;
        border: solid $primary;
    }
    #dwpa-body { height: auto; max-height: 1fr; }
    #dwpa-title {
        width: 1fr;
        text-align: center;
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #dwpa-capture { color: $text-muted; }
    .dwpa-label { margin-top: 1; }
    #dwpa-query-row { height: auto; margin-top: 1; }
    #dwpa-query, #dwpa-retrieve { min-width: 0; width: auto; }
    #dwpa-result { width: 1fr; margin-left: 1; min-height: 1; }
    #dwpa-buttons { align: right middle; height: auto; }
    """

    def __init__(self, capture: PersistedCapture, bssid: str, ssid: Optional[str],
                 save_keys: Optional[Callable[[Sequence[PotfileEntry]], str]] = None) -> None:
        """``save_keys`` stores the networks a Retrieve gets from the server and returns the
        status line; without it the dialog only reports how many were cracked."""
        super().__init__()
        self.capture = capture
        self.bssid = bssid
        self.ssid = ssid
        self._save_keys = save_keys

    def compose(self) -> ComposeResult:
        settings = Config.dwpa
        with Vertical(id="dwpa-dialog"):
            with VerticalScroll(id="dwpa-body"):
                yield Label("DWPA", id="dwpa-title")
                yield Label(f"Capture: {escape(Path(self.capture.path).name)}", id="dwpa-capture")
                yield Label("Server URL:", classes="dwpa-label")
                yield Input(settings.url or "", id="dwpa-url")
                yield Label("Key:")
                yield Input(settings.key or "", password=True, id="dwpa-key")
                yield Label(_KEY_HINT)
                with Horizontal(id="dwpa-query-row"):
                    yield Button("KQUERY", "primary", compact=True, id="dwpa-query")
                    yield Button("Retrieve", "primary", compact=True, id="dwpa-retrieve")
                    yield Label("", id="dwpa-result")
                with Horizontal(id="dwpa-buttons"):
                    yield Button("Cancel", variant="error", id="dwpa-cancel")
                    yield Button("Upload", variant="success", id="dwpa-upload")

    def on_mount(self) -> None:
        self.query_one("#dwpa-key" if Config.dwpa.url else "#dwpa-url", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#dwpa-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    @on(Button.Pressed, "#dwpa-upload")
    def _upload_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        url = self.query_one("#dwpa-url", Input).value.strip()
        key = self.query_one("#dwpa-key", Input).value.strip()
        if not url:
            self.notify("Please provide the DWPA server URL", severity="error")
            return
        if not is_valid_key(key):
            self.notify("The key must be 32 lowercase hex characters", severity="error")
            return
        self._remember(url, key)
        self._set_enabled(False)
        self.query_one("#dwpa-result", Label).update("[dim]Uploading...[/dim]")
        self._upload(url, key)

    @on(Button.Pressed, "#dwpa-query")
    def _query_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        url = self.query_one("#dwpa-url", Input).value.strip()
        if not url:
            self.notify("Please provide the DWPA server URL", severity="error")
            return
        if not self.ssid:
            self.notify("This capture has no SSID to query", severity="error")
            return
        self._remember(url, self.query_one("#dwpa-key", Input).value.strip())
        self.query_one("#dwpa-query", Button).disabled = True
        self.query_one("#dwpa-result", Label).update("[dim]Querying...[/dim]")
        self._query(url, self.ssid)

    @work(thread=True, exclusive=True, group="dwpa-query")
    def _query(self, url: str, ssid: str) -> None:
        try:
            leaked = network_leaked(url, self.bssid, ssid)
        except DwpaError as e:
            self.app.call_from_thread(self._show_query_failure, str(e))
            return
        self.app.call_from_thread(self._show_query_result, leaked)

    @on(Button.Pressed, "#dwpa-retrieve")
    def _retrieve_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        url = self.query_one("#dwpa-url", Input).value.strip()
        key = self.query_one("#dwpa-key", Input).value.strip()
        if not url:
            self.notify("Please provide the DWPA server URL", severity="error")
            return
        if not is_valid_key(key):
            self.notify("The key must be 32 lowercase hex characters", severity="error")
            return
        self._remember(url, key)
        self.query_one("#dwpa-retrieve", Button).disabled = True
        self.query_one("#dwpa-result", Label).update("[dim]Retrieving...[/dim]")
        self._retrieve(url, key)

    @work(thread=True, exclusive=True, group="dwpa-retrieve")
    def _retrieve(self, url: str, key: str) -> None:
        try:
            networks = retrieve_cracked(url, key)
        except DwpaError as e:
            self.app.call_from_thread(self._show_retrieve_failure, str(e))
            return
        self.app.call_from_thread(self._show_retrieved, networks)

    def _show_retrieve_failure(self, message: str) -> None:
        if not self.is_attached:
            return
        self.query_one("#dwpa-retrieve", Button).disabled = False
        self.query_one("#dwpa-result", Label).update(f"[$error]{escape(message)}[/]")

    def _show_retrieved(self, networks: Sequence[PotfileEntry]) -> None:
        if not self.is_attached:
            return
        self.query_one("#dwpa-retrieve", Button).disabled = False
        if not networks:
            self.query_one("#dwpa-result", Label).update("[dim]No cracked keys on DWPA[/]")
            return
        self._announce_selected_key(networks)
        if self._save_keys is None:
            message = f"{len(networks)} cracked key{'s' if len(networks) > 1 else ''} on DWPA"
        else:
            message = self._save_keys(networks)
        self.query_one("#dwpa-result", Label).update(f"[$success]{escape(message)}[/]")

    def _announce_selected_key(self, networks: Sequence[PotfileEntry]) -> None:
        mine = [network for network in networks if network.bssid == self.bssid]
        keys = tuple(dict.fromkeys(network.key for network in mine))
        if keys:
            self.notify(f"DWPA recovered the PSK for {mine[0].ssid}: {', '.join(keys)}",
                        title="DWPA")

    def _show_query_failure(self, message: str) -> None:
        if not self.is_attached:
            return
        self.query_one("#dwpa-query", Button).disabled = False
        self.query_one("#dwpa-result", Label).update(f"[$error]{escape(message)}[/]")

    def _show_query_result(self, leaked: bool) -> None:
        if not self.is_attached:
            return
        self.query_one("#dwpa-query", Button).disabled = False
        self.query_one("#dwpa-result", Label).update(_LEAKED if leaked else _NOT_LEAKED)

    @work(thread=True, exclusive=True, group="dwpa-upload")
    def _upload(self, url: str, key: str) -> None:
        try:
            upload_capture(url, key, self.capture.path)
        except DwpaError as e:
            self.app.call_from_thread(self._show_failure, str(e))
            return
        self.app.call_from_thread(self._show_success)

    def _set_enabled(self, enabled: bool) -> None:
        for selector in ("#dwpa-url", "#dwpa-key", "#dwpa-upload", "#dwpa-cancel"):
            self.query_one(selector).disabled = not enabled

    def _show_failure(self, message: str) -> None:
        if not self.is_attached:
            return
        self._set_enabled(True)
        self.query_one("#dwpa-result", Label).update(f"[$error]{escape(message)}[/]")

    def _show_success(self) -> None:
        if not self.is_attached:
            return
        self.dismiss(True)

    def _remember(self, url: str, key: str) -> None:
        settings = Config.dwpa
        settings.url = url
        settings.key = key
        try:
            Config.save()
        except ConfigError as e:
            self.notify(str(e), title="Config Error", severity="warning")
