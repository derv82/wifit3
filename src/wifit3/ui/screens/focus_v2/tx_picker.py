"""The TX-device picker: the card-name slot under the art, turned into a flat dropdown to pin which
card does injection (the pool honors the pin via ``WlanArray.prefer``). The list floats over the
layout as an ``overlay: screen`` child, the trick Textual's ``Select`` uses. ``build_rows`` is pure
(no Textual) so the disabled/warn/current logic is unit-tested directly."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from wifit3.wlan.array import fake_mac_rank
from wifit3.wlan.channels import band_label

from .art import display_name


@dataclass
class DeviceRow:
    """One pool card as the dropdown will render it."""
    iface: object
    prompt: Text
    disabled: bool      # can't reach the target's channel (wrong band)
    current: bool       # the card that will actually TX for this target


def _prompt(iface, *, disabled: bool, warn: bool, current: bool) -> Text:
    text = Text(no_wrap=True)
    if warn:
        text.append("! ", style="orange1 bold")   # plain ASCII: ⚠ is emoji-width and skews the row
    text.append(display_name(iface))
    if current:
        text.append("  ✓", style="green")
    elif disabled:
        band = band_label(list(getattr(iface, "supported_channels", []) or []))
        if band:
            text.append(f"  ({band})")
    return text


def build_rows(members, channel: Optional[int], current) -> List[DeviceRow]:
    """The dropdown's rows for ``members`` against a target ``channel`` (None = no target yet).
    A card is disabled when it can't tune to ``channel``; warned when a reachable peer is more
    MAC-capable; marked current when it is the elected TX card (``current``)."""
    reachable = [m for m in members if channel is None or channel in m.supported_channels]
    best = min((fake_mac_rank(m) for m in reachable), default=None)
    rows: List[DeviceRow] = []
    for m in members:
        disabled = channel is not None and channel not in m.supported_channels
        warn = (not disabled) and best is not None and fake_mac_rank(m) > best
        current_row = m is current
        rows.append(DeviceRow(m, _prompt(m, disabled=disabled, warn=warn, current=current_row),
                              disabled, current_row))
    return rows


class _DeviceOverlay(OptionList):
    """The floating list. Escape or losing focus (a click elsewhere) closes it via the picker."""
    BINDINGS = [Binding("escape", "dismiss", "close", show=False)]

    def action_dismiss(self) -> None:
        if isinstance(self.parent, TxDevicePicker):
            self.parent.close(refocus=True)

    def on_blur(self) -> None:
        if isinstance(self.parent, TxDevicePicker):
            self.parent.close(refocus=False)


class TxDevicePicker(Widget):
    DEFAULT_CSS = """
    TxDevicePicker { width: 20; height: 1; }
    TxDevicePicker #tx-overlay {
        display: none; width: 30; max-height: 8;
        overlay: screen; constrain: none inside;
        background: $surface; border: round $accent; padding: 0;
    }
    TxDevicePicker.-expanded #tx-overlay { display: block; }
    """

    BINDINGS = [
        Binding("enter", "open", "select device", show=False),
        Binding("space", "open", "select device", show=False),
    ]

    class Selected(Message):
        """User pinned a card as the TX device."""
        def __init__(self, iface) -> None:
            self.iface = iface
            super().__init__()

    def __init__(self, initial: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._text = initial
        self._members: list = []
        self._channel: Optional[int] = None
        self._current = None
        self._can_open = False
        self._open = False
        self._rows: List[DeviceRow] = []

    def compose(self) -> ComposeResult:
        yield Static(self._text, classes="card-static", id="card-chipset")
        yield _DeviceOverlay(id="tx-overlay")

    def sync(self, members, channel: Optional[int], current, locked: bool) -> None:
        """Refresh the trigger from the live pool + elected TX card. ``current`` is the peeked TX
        card (or the campaign's locked one); ``locked`` disables the dropdown during a campaign."""
        self._members = list(members)
        self._channel = channel
        self._current = current
        self._can_open = len(self._members) > 1 and not locked
        self.can_focus = self._can_open
        name = display_name(current) if current is not None else "no card"
        text = f"{name}  ▼" if self._can_open else name
        if self.is_mounted:
            if text != self._text:
                self.query_one("#card-chipset", Static).update(text)
            if not self._can_open and self._open:
                self.close(refocus=False)
        self._text = text

    def on_click(self) -> None:
        self.action_open()

    def action_open(self) -> None:
        if not self._can_open or self._open:
            return
        ol = self.query_one("#tx-overlay", _DeviceOverlay)
        ol.clear_options()
        self._rows = build_rows(self._members, self._channel, self._current)
        for row in self._rows:
            ol.add_option(Option(row.prompt, disabled=row.disabled))
        self._open = True
        self.add_class("-expanded")
        ol.display = True
        ol.focus()
        cur = next((i for i, r in enumerate(self._rows) if r.current and not r.disabled), None)
        if cur is not None:
            ol.highlighted = cur

    def close(self, *, refocus: bool) -> None:
        if not self._open:
            return
        self._open = False
        self.remove_class("-expanded")
        self.query_one("#tx-overlay", _DeviceOverlay).display = False
        if refocus and self._can_open:
            self.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        idx = event.option_index
        row = self._rows[idx] if 0 <= idx < len(self._rows) else None
        self.close(refocus=True)
        if row is not None and not row.disabled:
            self.post_message(self.Selected(row.iface))
