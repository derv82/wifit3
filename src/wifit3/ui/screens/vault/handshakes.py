from __future__ import annotations

from typing import Optional

from textual.content import Content
from textual.events import MouseMove
from textual.widgets import OptionList

from wifit3.persist.pcap import FourWayHandshake
from wifit3.text import displayable_ssid
from wifit3.ui.screens.vault.rows import row_content, verdict_column, verdict_tooltip


def captures_count_markup(count: int, total: int) -> str:
    return f"[dim]({count}/{total} captures)[/]"


def handshake_prompt(handshake: FourWayHandshake, show_client: bool, show_ssid: bool,
                     label_width: int) -> Content:
    """One row: date, messages, client, verdict, then the SSID when the AP had several."""
    trailing: list[tuple[str, str]] = []
    if show_ssid:
        if handshake.hashline is not None:
            trailing.append((displayable_ssid(bytes.fromhex(handshake.hashline.essid)), ""))
        else:
            trailing.append((handshake.ssid, ""))
    return row_content(int(handshake.first_seen), handshake.messages_label.ljust(label_width),
                       f"…{handshake.client_mac[-5:]}", verdict_column(handshake.authorized),
                       show_client, trailing)


class UncrackedHandshakeList(OptionList):
    """The uncracked 4-ways read from the AP's pcaps; the highlighted one is the selection."""

    def __init__(self, classes: str) -> None:
        super().__init__(classes=classes)
        self.handshakes: tuple[FourWayHandshake, ...] = ()

    def show(self, handshakes: tuple[FourWayHandshake, ...], prompts: list[Content]) -> None:
        """Replace the options, highlighting the first."""
        self.handshakes = handshakes
        self.tooltip = None
        self.clear_options()
        self.add_options(prompts)
        self.action_first()

    def on_mouse_move(self, event: MouseMove) -> None:
        index = event.style.meta.get("option")
        tooltip = None if index is None else verdict_tooltip(self.handshakes[index].authorized)
        if tooltip != self.tooltip:
            self.tooltip = tooltip

    def highlighted_handshake(self) -> Optional[FourWayHandshake]:
        return None if self.highlighted is None else self.handshakes[self.highlighted]
