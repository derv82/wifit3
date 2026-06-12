"""Uninstall confirmation modal. [DEVICE-SETUP.md]

Shown when the user presses the ✕ next to START. Removing access is the *safe* direction — it
locks the system back down — so the modal is framed calmly (cyan, not the alarm-red of a
destructive action). The body + scope choices are OS-specific:

  * Windows — one card's WinUSB binding (per-card; there's no "all"). ``"one"`` / ``None``.
  * Linux — the udev access rule, either just this card or every granted card (one shared file).
    ``"one"`` / ``"all"`` / ``None``.

A sticky help panel spells out exactly what each button removes. Dismisses with the scope string
(``"one"`` / ``"all"``) or ``None`` (cancel); Cancel holds focus so a stray Enter is harmless.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from wifit3.ui.screens._help_button import HelpButton, HelpRequest

_TITLE = {
    "win": "Remove Wifit3's WinUSB driver?",
    "linux": "Remove Wifit3's device access?",
}

_BODY = {
    "win": (
        "This removes the WinUSB driver Wifit3 installed for [bold]{name}[/], so Windows sees "
        "it as a normal wireless adapter again.\n[dim]Re-install any time with START.[/]"),
    "linux": (
        "This [orange1]removes Wifit3's udev access rule[/], revoking [italic]Wifit3's "
        "userland access[/] to its supported cards. This makes your system [green]more "
        "locked-down[/], not less — the only cost is granting access again next time.\n\n"
        "  • You can still run [bold]sudo wifit3[/] afterward.\n"
        "  • Does [bold]NOT[/] touch any other udev rule, or any non-Wifit3 device.\n"
        "  • Affected cards return to their normal Wi-Fi driver on the next replug."),
}


class ConfirmUninstallDialog(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    ConfirmUninstallDialog { align: center middle; }
    ConfirmUninstallDialog #dialog {
        width: auto; max-width: 92%; height: auto;
        border: thick cyan; background: $surface; padding: 1 2;
    }
    ConfirmUninstallDialog #title {
        width: 1fr; text-align: center; margin-bottom: 1; text-style: bold; color: cyan;
    }
    ConfirmUninstallDialog #body { width: 1fr; margin-bottom: 1; }
    ConfirmUninstallDialog #button-row { height: auto; align: center middle; margin-bottom: 1; }
    ConfirmUninstallDialog #button-row Button { margin: 0 1; }
    ConfirmUninstallDialog #btn-all { background: $panel; color: cyan; border: tall cyan; }
    ConfirmUninstallDialog #btn-all:hover { background: cyan 20%; }
    ConfirmUninstallDialog #help {
        width: 1fr; min-height: 3; border: round $primary-darken-1;
        padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self, description: str, os_kind: str, *, total_supported: int = 0) -> None:
        super().__init__()
        self._name = description
        self._short = description.split(" / ")[0][:18]
        self._os = os_kind if os_kind in _BODY else "win"
        self._total = total_supported
        self._help = self._help_text("one")

    def _help_text(self, key: str) -> str:
        from wifit3.setup.linux import RULE_PATH

        if key == "cancel":
            return "Exit without making changes. Your access rule stays as-is."
        if self._os == "win":
            return (f"Unbinds [bold]{self._short}[/] from WinUSB and restores its normal "
                    "Windows wireless driver — takes effect immediately.")
        if key == "all":
            return (f"Deletes [orange1]{RULE_PATH}[/] entirely — revokes access for "
                    f"[bold]all {self._total}[/] supported cards at once. Each returns to its "
                    "normal Wi-Fi driver on the next replug.")
        return (f"Removes [cyan bold]one rule[/] (the [bold]{self._short}[/] line) from "
                f"[orange1]{RULE_PATH}[/] and reloads udev. Every other granted card keeps its "
                "access. Replug this card to restore its kernel driver.")

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(_TITLE[self._os], id="title")
            yield Label(_BODY[self._os].format(name=self._name), id="body")
            with Horizontal(id="button-row"):
                if self._os == "linux":
                    yield HelpButton(f"Uninstall access: {self._short}",
                                     help_key="one", variant="success", id="btn-one")
                    yield HelpButton("Uninstall access: ALL cards",
                                     help_key="all", id="btn-all")
                else:
                    yield HelpButton("Uninstall WinUSB driver",
                                     help_key="one", variant="success", id="btn-one")
                yield HelpButton("Cancel", help_key="cancel", variant="default", id="btn-cancel")
            yield Static(self._help, id="help")

    def on_mount(self) -> None:
        self.query_one("#btn-cancel", Button).focus()  # default to the safe choice

    def on_help_request(self, event: HelpRequest) -> None:
        self.query_one("#help", Static).update(self._help_text(event.key))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choice = {"btn-one": "one", "btn-all": "all"}.get(event.button.id or "")
        self.dismiss(choice)

    def action_cancel(self) -> None:
        self.dismiss(None)
