"""Preview the Focus v2 LOG / CLIENTS panel chrome in three border colours.

Claude Code's in-terminal renderer shows panel borders/titles in monochrome, so
this prints the candidates (matrix green = shipped, plus cyan + grey) to *your*
terminal with real colour. Run it in Windows Terminal / any truecolor TTY:

    uv run python scripts/ui/preview_focus_chrome.py
    # or just double-click / run: scripts/ui/preview_focus_chrome.bat

It also shows the cyan ESSID chip + the red 'Deauth all' button so you can judge
the whole bottom band's contrast at once. To switch the app, edit `_BORDER` in
src/wifit3/ui/screens/focus_v2/screen.py to the colour you prefer.
"""
from __future__ import annotations

import sys

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

# (label, colour) — keep the first in sync with screen.py:_BORDER.
CANDIDATES = [("Cyan (shipped)", "cyan"), ("Matrix green", "#00ff00"), ("Grey", "grey50")]

_LOG_LINES = [
    "[dim]19:42:01[/dim]  [bold]Target acquired:[/bold] [black bold on cyan] NETGEAR91 [/]",
    "[dim]19:42:01[/dim]   [dim]├─►[/dim] [dim]Encryption:[/dim] WPA2",
    "[dim]19:42:04[/dim]  [black bold on green] ✓ Valid 4-Way Handshake (M1+M2) [/]",
]
_CLIENT_ROWS = [
    "fa:11:22:33:44:aa  -79   10   [white on red] ✕ [/]",
    "9c:b6:d0:1a:2b:3c  -67  512   [white on red] ✕ [/]",
]


def _panel(title: str, colour: str, body) -> Panel:
    return Panel(
        body, title=Text(title, style=f"bold {colour}"), title_align="left",
        border_style=colour, box=box.ROUNDED, width=44, padding=(0, 1),
    )


def main() -> None:
    # The box-drawing + ✓/✕ glyphs need UTF-8; force it so a cp1252 console
    # (or a redirected pipe) shows the art instead of a UnicodeEncodeError.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    console = Console()
    console.print()
    for label, colour in CANDIDATES:
        console.rule(Text(label, style=f"bold {colour}"))
        log = _panel("LOG", colour, Group(*[Text.from_markup(ln) for ln in _LOG_LINES]))
        clients_body = Group(
            Text.from_markup("[white on red]          Deauth all          [/]"),
            *[Text.from_markup(r) for r in _CLIENT_ROWS],
        )
        clients = _panel("CLIENTS (5)", colour, clients_body)
        console.print(log)
        console.print(clients)
        console.print()


if __name__ == "__main__":
    main()
