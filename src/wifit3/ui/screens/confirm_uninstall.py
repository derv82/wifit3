"""Uninstall confirmation modal. [DEVICE-SETUP.md]

Shown when the user presses the ✕ next to START. Asks to remove wifit3's driver/access change
for the selected card. The body copy is OS-specific (Windows WinUSB unbind vs Linux udev-rule
removal) — passed via ``os_kind``. Dismisses ``True`` (uninstall) / ``False`` (cancel); the
safe choice (Cancel) holds focus so a stray Enter doesn't uninstall.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

# Per-OS explanation of what removal does. Windows is an immediate, per-card driver swap; Linux
# drops the single shared permission rule (covering ALL supported cards) and each card's kernel
# driver returns on replug (we never replaced it). The Linux copy ignores {name} — the rule is
# blanket, not per-card — but the placeholder is accepted so both share one .format() call.
_BODY = {
    "win": (
        "This removes the WinUSB driver wifit3 installed for [bold]{name}[/], so Windows "
        "sees it as a normal wireless adapter again.\n[dim]You can re-install it any time "
        "with START.[/dim]"),
    "linux": (
        "This removes wifit3's udev access rule, which covers [bold]all supported cards[/] "
        "(it's one shared rule).\n[dim]Each card returns to its normal Wi-Fi driver on the "
        "next replug.[/dim]"),
}


class ConfirmUninstallDialog(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "Uninstall", show=True),
        Binding("n", "no", "Cancel", show=True),
        Binding("escape", "no", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    ConfirmUninstallDialog { align: center middle; }
    ConfirmUninstallDialog #dialog {
        width: 64; max-width: 90%; height: auto;
        border: thick $error; background: $surface; padding: 1 2;
    }
    ConfirmUninstallDialog #title {
        width: 1fr; text-align: center; margin-bottom: 1; text-style: bold; color: $error;
    }
    ConfirmUninstallDialog #body { width: 1fr; text-align: center; margin-bottom: 1; }
    ConfirmUninstallDialog #button-row { height: auto; align: center middle; }
    ConfirmUninstallDialog #button-row Button { margin: 0 2; }
    """

    def __init__(self, description: str, os_kind: str) -> None:
        super().__init__()
        self._name = description
        self._os_kind = os_kind if os_kind in _BODY else "win"

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Uninstall wifit3 driver?", id="title")
            yield Label(_BODY[self._os_kind].format(name=self._name), id="body")
            with Horizontal(id="button-row"):
                yield Button("Uninstall", variant="error", id="btn-yes")
                yield Button("Cancel", variant="default", id="btn-no")

    def on_mount(self) -> None:
        self.query_one("#btn-no", Button).focus()  # default to the safe choice

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)
