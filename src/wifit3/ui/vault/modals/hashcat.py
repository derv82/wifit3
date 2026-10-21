from textual import on
from textual.app import ComposeResult
from textual.widgets import Checkbox, Input, Label

from wifit3.persist.config import Config, HashcatSettings
from wifit3.ui.path_picker import PathInput
from wifit3.ui.vault.modals.wordlists import WordlistConfigModal
import shutil
import sys
from typing import Any, Optional

_SELF_TEST_DISABLE = "--self-test-disable"


def _default_hashcat_path() -> str:
    """A saved path wins; otherwise find hashcat on PATH, then guess per platform."""
    if Config.hashcat.path:
        return Config.hashcat.path
    if found := shutil.which("hashcat"):
        return found
    if sys.platform == "win32":
        return r"C:\hashcat\hashcat.exe"
    if sys.platform == "darwin":
        return "/opt/homebrew/bin/hashcat"
    return "/usr/bin/hashcat"


class HashcatConfigModal(WordlistConfigModal):
    """Configuration modal for Hashcat jobs."""

    dialog_title = "Hashcat"

    DEFAULT_CSS = """
    .hashcat-label {
        margin-top: 1;
    }
    """

    def _settings(self) -> HashcatSettings:
        return Config.hashcat

    def _compose_fields(self) -> ComposeResult:
        yield Label("Hashcat Executable Path:", classes="hashcat-label")
        yield PathInput(_default_hashcat_path(), title="Select hashcat executable",
                        id="hashcat-exe")

        yield from self._compose_wordlists()

        args = Config.hashcat.args or ""
        yield Label("Hashcat Args:", classes="hashcat-label")
        yield Input(args, placeholder="custom hashcat args (e.g. -a 0 -w 3)",
                    id="hashcat-args")
        yield Checkbox(_SELF_TEST_DISABLE, value=_SELF_TEST_DISABLE in args,
                       id="hashcat-self-test-disable")

    @on(Checkbox.Changed, "#hashcat-self-test-disable")
    def _toggle_self_test_disable(self, event: Checkbox.Changed) -> None:
        field = self.query_one("#hashcat-args", Input)
        args = field.value.strip()
        if not event.value:
            field.value = args.replace(_SELF_TEST_DISABLE, "").strip()
        elif _SELF_TEST_DISABLE not in args:
            field.value = f"{args} {_SELF_TEST_DISABLE}".strip()

    def _launch_config(self) -> Optional[dict[str, Any]]:
        exe = self.query_one("#hashcat-exe", Input).value.strip()
        if not exe:
            self.notify("Please provide the hashcat executable", severity="error")
            return None
        config = super()._launch_config()
        if config is None:
            return None
        return {"hashcat_exe": exe, **config,
                "hashcat_args": self.query_one("#hashcat-args", Input).value.strip()}

    def _store_settings(self, config: dict[str, Any]) -> None:
        super()._store_settings(config)
        Config.hashcat.path = config["hashcat_exe"]
        Config.hashcat.args = config["hashcat_args"]
