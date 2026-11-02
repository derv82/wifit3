"""The Verify button's dropdown: one row per stored credential of the target."""
from __future__ import annotations

from typing import Optional

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.content import Content
from textual.screen import ModalScreen
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from wifit3.campaigns.verify import VERIFY_CAMPAIGNS
from wifit3.persist.ap_state import Key
from wifit3.ui.screens.vault_item import date_text, key_text

def key_option_prompts(keys: list[Key]) -> list[Content]:
    """``PSK: hunter22    Sep 23 05:06``: type and value left, the date right-aligned and dim."""
    values = [f"{VERIFY_CAMPAIGNS[key.type].credential_name}: {key_text(key)}" for key in keys]
    dates = [date_text(key.newest_capture.timestamp) for key in keys]
    value_width = max(len(value) for value in values)
    date_width = max(len(date) for date in dates)
    return [Content.assemble(value.ljust(value_width + 4), (date.rjust(date_width), "dim"))
            for value, date in zip(values, dates)]


class VerifyKeyMenu(ModalScreen[Optional[Key]]):
    """Dismisses with the chosen key, or None."""

    BINDINGS = [Binding("escape", "dismiss", "Close", show=False)]

    DEFAULT_CSS = """
    VerifyKeyMenu { background: $background 0%; }
    VerifyKeyMenu > OptionList {
        width: auto; height: auto; max-height: 12; border: round $primary; background: $panel;
    }
    """

    def __init__(self, keys: list[Key], *, offset: tuple[int, int]) -> None:
        super().__init__()
        self._keys = keys
        self._offset = offset

    def compose(self) -> ComposeResult:
        options = OptionList(*(Option(prompt) for prompt in key_option_prompts(self._keys)))
        options.styles.offset = self._offset
        yield options

    def on_mount(self) -> None:
        self.call_after_refresh(self._keep_on_screen)

    def _keep_on_screen(self) -> None:
        options = self.query_one(OptionList)
        max_x = max(0, self.size.width - options.outer_size.width)
        max_y = max(0, self.size.height - options.outer_size.height)
        x, y = self._offset
        options.styles.offset = (max(0, min(x, max_x)), max(0, min(y, max_y)))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._keys[event.option_index])

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:
            self.dismiss(None)
