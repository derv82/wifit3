"""Event log — bottom-left, bordered. Fixed/capped width (the hard-won
<40-char log lines justified); it narrates and never needs to expand."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Label


class LogBand(Vertical):
    def __init__(self, lines, **kwargs) -> None:
        super().__init__(**kwargs)
        self._lines = lines

    def compose(self) -> ComposeResult:
        for line in self._lines:
            yield Label(line, classes="log-line")

    def on_mount(self) -> None:
        self.border_title = "LOG"
