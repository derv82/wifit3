"""Linux device-access grant modal — the connect-failure path's "needs setup" UX. [DEVICE-SETUP.md]

Shown when the user STARTs a supported card whose usbfs node isn't writable yet. Reuses the
install dialog's Wifit3 ◄──► Device Access ◄──► card diagram (the access is the missing REQUIRED
link), then lets the user choose the *scope* of the one-time udev rule:

  * Grant this card only (green) → ``"one"``
  * Grant all supported cards (cyan) → ``"all"``  (hot-plug any card later, no further prompt)
  * Cancel (neutral) → ``None``

A sticky help panel under the buttons spells out exactly what the highlighted button will write
— the verbatim udev rule, the file path, the deduped count — because the audience reads udev
rules and deserves the literal change, not a summary. Dismisses with ``"one"`` / ``"all"`` /
``None``.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from wifit3.ui.screens._device_chain import PULSE
from wifit3.ui.screens._device_chain import chain_diagram
from wifit3.ui.screens._help_button import HelpButton, HelpRequest

_TITLE = "Wifit3 needs one-time access to talk to this (and other) wireless cards"


def _rule_line(vid: int, pid: int, desc: str) -> str:
    """One verbatim udev rule line, colour-tokenised for the help panel (matches what
    :func:`wifit3.setup.linux.build_rule_text` writes, character for character)."""
    return (
        f"[dim]# {desc}[/]\n"
        f'[cyan]SUBSYSTEM[/]==[orange1]"usb"[/], '
        f'[cyan]ATTR{{idVendor}}[/]==[orange1]"{vid:04x}"[/], '
        f'[cyan]ATTR{{idProduct}}[/]==[orange1]"{pid:04x}"[/], '
        f'[cyan]TAG[/]+=[orange1]"uaccess"[/], '
        f'[orange1]MODE="0660"[/], [orange1]GROUP="plugdev"[/]')


def _members_line(members: list[str]) -> str:
    """The plugdev transparency line: first three members + overflow, or a uaccess-only note."""
    if not members:
        return "[dim](no plugdev members — uaccess covers you on this box)[/]"
    shown = ", ".join(members[:3])
    extra = len(members) - 3
    tail = f" [dim]+{extra} more[/]" if extra > 0 else ""
    you = " [dim](← that's just you)[/]" if len(members) == 1 else ""
    return f"[bold]{shown}[/]{tail}{you}"


class ConfirmAccessDialog(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    ConfirmAccessDialog { align: center middle; }
    ConfirmAccessDialog #dialog {
        width: auto; max-width: 96%; height: auto;
        border: thick $primary; background: $surface; padding: 1 2;
    }
    ConfirmAccessDialog #title {
        width: 1fr; text-align: center; margin-bottom: 1; text-style: bold;
    }
    ConfirmAccessDialog #diagram { content-align: center middle; margin-bottom: 1; }
    ConfirmAccessDialog #body { width: 1fr; margin-bottom: 1; }
    ConfirmAccessDialog #button-row { height: auto; align: center middle; margin-bottom: 1; }
    ConfirmAccessDialog #button-row Button { margin: 0 1; }
    /* Cyan "all cards" button — Button has no cyan variant, so style by id. */
    ConfirmAccessDialog #btn-all { background: $panel; color: cyan; border: tall cyan; }
    ConfirmAccessDialog #btn-all:hover { background: cyan 20%; }
    ConfirmAccessDialog #help {
        width: 1fr; min-height: 5; border: round $primary-darken-1;
        padding: 0 1; color: $text-muted;
    }
    """

    def __init__(self, description: str, vid: int, pid: int, *, total_supported: int,
                 plugdev_members: list[str]) -> None:
        super().__init__()
        self._full = description
        self._short = description.split(" / ")[0][:18]
        self._vid, self._pid = vid, pid
        self._total = total_supported
        self._members = plugdev_members
        self._pulse_i = 0
        self._help = self._help_text("one")  # never blank; matches the initially-focused button

    def _help_text(self, key: str) -> str:
        from wifit3.setup.linux import RULE_PATH

        if key == "one":
            return (
                f"Appends [cyan bold]one rule[/] to [orange1]{RULE_PATH}[/]:\n\n"
                f"  {_rule_line(self._vid, self._pid, self._full)}\n\n"
                "Grants [bold]this card only[/]. Any other supported card you plug in later "
                "stays kernel-bound until you grant it too.")
        if key == "all":
            return (
                f"Writes [cyan]{self._total} rules[/] to [orange1]{RULE_PATH}[/] — one per "
                "supported card. Example:\n\n"
                f"  {_rule_line(self._vid, self._pid, self._full)}\n"
                f"  [dim]# … {self._total - 1} more[/]\n\n"
                "Hot-plug [bold]any[/] supported card afterward with no further prompt. Same "
                "perms, just every card.")
        return ("Exit without making changes. No rule is written; the card stays kernel-bound. "
                "[dim](You can still run [bold]sudo wifit3[/] to use it anyway.)[/]")

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(_TITLE, id="title")
            yield Static(
                chain_diagram(self._short, "Device Access", PULSE[0],
                              extra_supported=f"+ {self._total - 1} other supported devices"),
                id="diagram")
            yield Label(self._body_markup(), id="body")
            with Horizontal(id="button-row"):
                yield HelpButton(f"Grant access: {self._short} only",
                                 help_key="one", variant="success", id="btn-one")
                yield HelpButton(f"Grant access: all USB Wi-Fi ({self._total})",
                                 help_key="all", id="btn-all")
                yield HelpButton("Cancel", help_key="cancel", variant="default", id="btn-cancel")
            yield Static(self._help, id="help")

    def _body_markup(self) -> str:
        return (
            "This is a [orange1]one-time setup[/]. Wifit3 writes a udev rule granting userland "
            "access to supported wireless cards — to two sets of people, [bold]nobody else[/]:\n"
            "  [cyan]uaccess[/]  — you, while logged in at this machine. Auto-revoked at logout. "
            "[dim]Excludes SSH.[/]\n"
            "  [cyan]plugdev[/] — members of the plugdev group, [bold]including over SSH[/].\n"
            f"            Current members: {_members_line(self._members)}\n\n"
            "  [green]✓[/] [green]Cards keep working as normal Wi-Fi[/] — Wifit3 only takes a "
            "card at runtime (replug undoes).\n"
            "  [green]✓[/] [green]No sudo[/] — userland, even when you SSH in.\n"
            "  [green]✓[/] [green]Reversible[/] — press [bold]✕[/] anytime to remove the rule.")

    def on_mount(self) -> None:
        self.query_one("#btn-one", Button).focus()
        self.set_interval(0.16, self._pulse)

    def _pulse(self) -> None:
        self._pulse_i = (self._pulse_i + 1) % len(PULSE)
        self.query_one("#diagram", Static).update(
            chain_diagram(self._short, "Device Access", PULSE[self._pulse_i],
                          extra_supported=f"+ {self._total - 1} other supported devices"))

    def on_help_request(self, event: HelpRequest) -> None:
        self.query_one("#help", Static).update(self._help_text(event.key))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choice = {"btn-one": "one", "btn-all": "all"}.get(event.button.id or "")
        self.dismiss(choice)

    def action_cancel(self) -> None:
        self.dismiss(None)
