"""A Button that asks the dialog to show its sticky help text on hover OR focus.

The access / uninstall modals describe exactly what each button's privileged action will do
(the verbatim udev rule, the file path, who's affected) in a panel below the buttons. The text
is *sticky*: it updates when the pointer enters a button or the button gains keyboard focus, and
stays put until another button takes over — so a keyboard user tabbing through gets the same
explanation a mouse user gets hovering. The button posts :class:`HelpRequest`; the host modal
handles it and updates the panel.
"""
from __future__ import annotations

from textual import events
from textual.message import Message
from textual.widgets import Button


class HelpRequest(Message):
    """Posted by a :class:`HelpButton` when it's hovered or focused. ``key`` selects the help
    blurb the host modal should display."""
    def __init__(self, key: str) -> None:
        super().__init__()
        self.key = key


class HelpButton(Button):
    """A :class:`~textual.widgets.Button` carrying a help ``key`` it advertises on hover/focus."""

    def __init__(self, label: str, *, help_key: str, **kwargs) -> None:
        super().__init__(label, **kwargs)
        self._help_key = help_key

    def _advertise(self) -> None:
        self.post_message(HelpRequest(self._help_key))

    def on_enter(self, event: events.Enter) -> None:   # pointer hover
        self._advertise()

    def on_focus(self, event: events.Focus) -> None:   # keyboard focus
        self._advertise()
