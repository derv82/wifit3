"""Uninstall confirmation modal.

Shown when the user presses the ✕ next to START. Asks to remove wifit3's driver/access change
for the selected card. The body copy is OS-specific (Windows WinUSB unbind vs Linux udev-rule
removal) — passed via ``os_kind``.

Linux only: one kernel module can back several handed-over cards (rt5372, rt3070, rt2800usb all
bind rt2800usb.ko). When such siblings exist the dialog offers **two radii** — remove just this
card, or this card and the related ones so the shared module is actually freed. Dismisses
``"narrow"`` / ``"wide"`` / ``None`` (cancel); the safe choice (Cancel) holds focus so a stray
Enter doesn't uninstall.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

# Per-OS explanation of what removal does. Windows is an immediate per-card WinUSB→native driver
# swap; Linux deletes this chipset's blacklist + access-rule pair, so the kernel Wi-Fi driver
# rebinds the card on its next replug.
_BODY = {
    "win": (
        "This removes the WinUSB driver wifit3 installed for [bold]{name}[/], so Windows "
        "sees it as a normal wireless adapter again.\n[dim]You can re-install it any time "
        "with START.[/dim]"),
    "linux": (
        "This uninstalls Wifit3's udev and modprobe rules for the [bold]{name}[/].\n\n"
        "[dim]The card returns to normal on the next unplug/replug.[/dim]"),
}

_TITLE = {
    "win": "Uninstall wifit3 driver?",
    "linux": "Uninstall Wifit3's udev + modprobe rules?",
}


class ConfirmUninstallDialog(ModalScreen[str | None]):
    BINDINGS = [
        Binding("n", "cancel", "Cancel", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    ConfirmUninstallDialog { align: center middle; }
    ConfirmUninstallDialog #dialog {
        width: 68; max-width: 90%; height: auto;
        border: thick cyan; background: $surface; padding: 1 2;
    }
    ConfirmUninstallDialog #title {
        width: 1fr; text-align: center; margin-bottom: 1; text-style: bold; color: cyan;
    }
    ConfirmUninstallDialog #body { width: 1fr; text-align: center; margin-bottom: 1; }
    ConfirmUninstallDialog #button-row { height: auto; align: center middle; }
    ConfirmUninstallDialog #button-row Button { margin: 0 1; }
    ConfirmUninstallDialog #btn-narrow, ConfirmUninstallDialog #btn-wide { background: cyan; color: black; }
    """

    def __init__(self, description: str, os_kind: str, *,
                 siblings: list[str] | None = None, has_own_files: bool = True) -> None:
        super().__init__()
        self._name = description
        self._os_kind = os_kind if os_kind in _BODY else "win"
        self._siblings = list(siblings or [])
        self._has_own = has_own_files

    def _body_text(self) -> str:
        base = _BODY[self._os_kind].format(name=self._name)
        if not self._siblings:
            return base
        # Linux shared-module case: name the related cards (never cryptic module names) and explain
        # the two radii. The related cards' kernel driver stays blocked until they're uninstalled too.
        others = ", ".join(f"[bold]{s}[/]" for s in self._siblings)
        n = len(self._siblings)
        shared = (f"\n\n[$text-warning]This card shares its kernel driver with "
                  f"{n} other card{'s' if n != 1 else ''} you've handed to wifit3:[/] {others}.")
        if self._has_own:
            return (base + shared +
                    "\n\n[dim]“This card only” leaves the driver blocked for the others until you "
                    "uninstall them too. “All …” frees it for the whole group.[/dim]")
        # Card has no rules of its own — a sibling is what's blocking it.
        return (f"[bold]{self._name}[/] has no Wifit3 rules of its own, but its kernel driver is "
                f"blocked by {n} card{'s' if n != 1 else ''} you've handed to wifit3: {others}."
                "\n\n[dim]Uninstalling those returns this card to normal on the next replug.[/dim]")

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(_TITLE[self._os_kind], id="title")
            yield Label(self._body_text(), id="body")
            with Horizontal(id="button-row"):
                total = 1 + len(self._siblings)
                if self._siblings and self._has_own:
                    yield Button("This card only", variant="primary", id="btn-narrow")
                    yield Button(f"All {total} cards", variant="primary", id="btn-wide")
                elif self._siblings:                       # blocked-by-sibling, nothing of its own
                    yield Button(f"Uninstall {total} cards", variant="primary", id="btn-wide")
                else:                                      # plain single-card uninstall
                    yield Button("Uninstall", variant="primary", id="btn-narrow")
                yield Button("Cancel", variant="default", id="btn-cancel")

    def on_mount(self) -> None:
        self.query_one("#btn-cancel", Button).focus()  # default to the safe choice

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss({"btn-narrow": "narrow", "btn-wide": "wide"}.get(event.button.id))

    def action_cancel(self) -> None:
        self.dismiss(None)
