"""Event log — bottom-left, bordered. A live ``RichLog`` (markup, scrolling) the
screen appends to as capture events land; the hard-won <40-char log lines mean
it narrates without ever needing to expand. The shell seeds it with demo lines
(no-target screenshots); a live target clears it and writes the real stream."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog


class LogBand(Vertical):
    def __init__(self, lines, **kwargs) -> None:
        super().__init__(**kwargs)
        self._initial = lines

    def compose(self) -> ComposeResult:
        yield RichLog(id="log-rich", markup=True, highlight=False, wrap=True)

    def on_mount(self) -> None:
        self.border_title = "LOG"
        log = self.query_one("#log-rich", RichLog)
        for line in self._initial:
            log.write(line)

    def write(self, renderable) -> None:
        self.query_one("#log-rich", RichLog).write(renderable)

    def clear(self) -> None:
        self.query_one("#log-rich", RichLog).clear()
