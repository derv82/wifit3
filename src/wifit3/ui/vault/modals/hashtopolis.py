from typing import Any, Optional

from rich.markup import escape
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select

from wifit3.persist.config import Config, ConfigError
from wifit3.vault.tools.hashtopolis_api import (AccessGroup, HashtopolisApi, HashtopolisError,
                                                ServerFile)

_ACCESS_KEY_HINT = "[dim]Use port 8080 (legacy API), [i]not 4200[/i]: Users → API Management[/dim]"
_REFRESH_HINT = "[dim]Refresh to list wordlists[/dim]"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


class HashtopolisConfigModal(ModalScreen[dict]):
    """Server, access key, wordlist and access group for Hashtopolis jobs; dismisses with the
    job's ``config``, or None when cancelled."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    # CSS, not DEFAULT_CSS: the app's `Button { min-width: 12 }` outranks any DEFAULT_CSS rule.
    CSS = """
    #hashtopolis-reveal { min-width: 0; width: auto; }
    #hashtopolis-test { min-width: 0; width: auto; }
    """

    DEFAULT_CSS = """
    HashtopolisConfigModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.5);
    }
    #hashtopolis-dialog {
        width: 64;
        max-width: 100%;
        height: auto;
        padding: 0 1;
        background: $surface;
        border: solid $primary;
    }
    #hashtopolis-title {
        width: 1fr;
        text-align: center;
        text-style: bold;
        color: $accent;
    }
    #hashtopolis-dialog Horizontal { height: auto; }
    #hashtopolis-access-key-label { width: 1fr; }
    #hashtopolis-test-row { margin-top: 1; }
    #hashtopolis-test-result { width: 1fr; margin-left: 1; }
    .hashtopolis-target-label { width: 10; height: 3; content-align: left middle; }
    .hashtopolis-target-row Select { width: 1fr; }
    #hashtopolis-buttons { align: right middle; }
    """

    def __init__(self, confirm_label: str = "Launch") -> None:
        """``confirm_label`` "Save" configures the tool without the caller launching a job."""
        super().__init__()
        self._confirm_label = confirm_label
        settings = Config.hashtopolis
        self._wordlists: dict[int, str] = {}
        if settings.wordlist_id:
            self._wordlists[settings.wordlist_id] = settings.wordlist_name or ""
        self._groups: dict[int, str] = {}
        if settings.access_group_id:
            self._groups[settings.access_group_id] = (settings.access_group_name
                                                      or str(settings.access_group_id))

    def compose(self) -> ComposeResult:
        with Vertical(id="hashtopolis-dialog"):
            yield Label("Hashtopolis", id="hashtopolis-title")
            yield from self._compose_server()
            yield from self._compose_test()
            yield from self._compose_targets()
            with Horizontal(id="hashtopolis-buttons"):
                yield Button("Cancel", variant="error", id="hashtopolis-cancel")
                yield Button(self._confirm_label, variant="success", id="hashtopolis-confirm")

    def _compose_server(self) -> ComposeResult:
        settings = Config.hashtopolis
        yield Label("Server URL:")
        yield Input(settings.url or "", placeholder="http://192.168.1.10:8080",
                    id="hashtopolis-url")
        with Horizontal():
            yield Label("Access key:", id="hashtopolis-access-key-label")
            yield Button("reveal", compact=True, id="hashtopolis-reveal")
        yield Input(settings.access_key or "", password=True,
                    id="hashtopolis-access-key")
        yield Label(_ACCESS_KEY_HINT)

    def _compose_test(self) -> ComposeResult:
        with Horizontal(id="hashtopolis-test-row"):
            yield Button("Test / Refresh", "primary", compact=True, id="hashtopolis-test")
            yield Label("" if self._wordlists else _REFRESH_HINT, id="hashtopolis-test-result")

    def _compose_targets(self) -> ComposeResult:
        settings = Config.hashtopolis
        with Horizontal(classes="hashtopolis-target-row"):
            yield Label("Wordlist:", classes="hashtopolis-target-label")
            yield Select([(name, file_id) for file_id, name in self._wordlists.items()],
                         prompt="Choose a wordlist", value=settings.wordlist_id or Select.NULL,
                         id="hashtopolis-wordlist")
        with Horizontal(classes="hashtopolis-target-row"):
            yield Label("Group:", classes="hashtopolis-target-label")
            yield Select([(name, group_id) for group_id, name in self._groups.items()],
                         prompt="Choose an access group",
                         value=settings.access_group_id or Select.NULL,
                         id="hashtopolis-access-group")

    @on(Button.Pressed, "#hashtopolis-reveal")
    def _toggle_reveal(self, event: Button.Pressed) -> None:
        event.stop()
        access_key = self.query_one("#hashtopolis-access-key", Input)
        access_key.password = not access_key.password
        event.button.label = "reveal" if access_key.password else "hide"

    @on(Button.Pressed, "#hashtopolis-test")
    def _test_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.query_one("#hashtopolis-test-result", Label).update("[dim]Connecting...[/dim]")
        self._test(self.query_one("#hashtopolis-url", Input).value.strip(),
                   self.query_one("#hashtopolis-access-key", Input).value.strip())

    @work(thread=True, exclusive=True, group="hashtopolis-test")
    def _test(self, url: str, access_key: str) -> None:
        try:
            api = HashtopolisApi(url, access_key)
            api.check_access()
            wordlists = api.wordlists()
            groups = api.access_groups()
        except HashtopolisError as e:
            self.app.call_from_thread(self._show_test_failure, str(e))
            return
        self.app.call_from_thread(self._show_server_lists, wordlists, groups)

    def _show_test_failure(self, message: str) -> None:
        if not self.is_attached:
            return
        self.query_one("#hashtopolis-test-result", Label).update(f"[$error]{escape(message)}[/]")

    def _show_server_lists(self, wordlists: list[ServerFile], groups: list[AccessGroup]) -> None:
        if not self.is_attached:
            return
        result = self.query_one("#hashtopolis-test-result", Label)
        if wordlists:
            result.update(f"[$success]Connected · {_plural(len(wordlists), 'wordlist')} · "
                          f"{_plural(len(groups), 'group')}[/]")
        else:
            result.update("[$warning]Connected · no wordlists found[/]")
        self._wordlists = {f.file_id: f.filename for f in wordlists}
        self._groups = {g.group_id: g.name for g in groups}
        self._fill(self.query_one("#hashtopolis-wordlist", Select),
                   [(f.filename, f.file_id) for f in wordlists])
        self._fill(self.query_one("#hashtopolis-access-group", Select),
                   [(g.name, g.group_id) for g in groups])

    @staticmethod
    def _fill(select: Select, options: list[tuple[str, int]]) -> None:
        chosen = select.value
        select.set_options(options)
        values = [value for _, value in options]
        if chosen in values:
            select.value = chosen
        elif len(values) == 1:
            select.value = values[0]

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#hashtopolis-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    @on(Button.Pressed, "#hashtopolis-confirm")
    def _confirm(self, event: Button.Pressed) -> None:
        event.stop()
        url = self.query_one("#hashtopolis-url", Input).value.strip()
        access_key = self.query_one("#hashtopolis-access-key", Input).value.strip()
        if not (url and access_key):
            self.notify("Please provide the server URL and access key", severity="error")
            return
        config = self._launch_config()
        if config is None:
            return
        self._remember(url, access_key, config)
        self.dismiss(config)

    def _launch_config(self) -> Optional[dict[str, Any]]:
        """The job's config; the server URL and access key stay in Config, out of jobs.json."""
        wordlist_id = self.query_one("#hashtopolis-wordlist", Select).selection
        group_id = self.query_one("#hashtopolis-access-group", Select).selection
        if wordlist_id is None or group_id is None:
            self.notify("Please refresh and choose a wordlist and access group", severity="error")
            return None
        return {"wordlist_id": wordlist_id, "wordlist_name": self._wordlists.get(wordlist_id, ""),
                "access_group_id": group_id}

    def _remember(self, url: str, access_key: str, config: dict[str, Any]) -> None:
        settings = Config.hashtopolis
        settings.url = url
        settings.access_key = access_key
        settings.wordlist_id = config["wordlist_id"]
        settings.wordlist_name = config["wordlist_name"]
        settings.access_group_id = config["access_group_id"]
        settings.access_group_name = self._groups.get(config["access_group_id"])
        try:
            Config.save()
        except ConfigError as e:
            self.notify(str(e), title="Config Error", severity="warning")
