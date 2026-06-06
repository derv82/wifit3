"""Eyeball the reception-quality signal bar at every rate, in real colour.

    uv run python scripts/signal_bar_preview.py

No hardware, no TUI — just renders the bar (``src/wifit3/ui/signal_bar.py``)
across the beacons/s range so the actual ANSI gradient, glyphs, and dead-AP
heartbeat can be judged before it's wired into the Focus view.
"""
from __future__ import annotations

import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rich.console import Console  # noqa: E402
from rich.text import Text  # noqa: E402

from wifit3.ui.signal_bar import render_signal_bar  # noqa: E402

# The block glyphs aren't cp1252; force UTF-8 so this renders on a stock Windows
# console (and when stdout is piped) as well as in a UTF-8 terminal.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

console = Console()


def _row(label: str, bar: Text) -> Text:
    row = Text(f"{label:>7}  ", no_wrap=True)
    row.append_text(bar)
    return row


def main() -> None:
    for width in (10, 16):
        console.rule(f"live meter — width {width}")
        console.print(_row("…/s", render_signal_bar(None, width=width)))
        for rate in (0.0, 0.4, 1.0, 2.1, 3.5, 5.0, 6.3, 7.0, 8.5, 9.8, 12.0):
            console.print(_row(f"{rate:.1f}/s", render_signal_bar(rate, width=width)))
        console.print()

    console.rule("dead-AP heartbeat (1s dim-pulse + red ╳)")
    for pulse in (1.0, 0.66, 0.33, 0.0, 0.33, 0.66, 1.0):
        console.print(_row("dead", render_signal_bar(0.0, width=10, pulse=pulse)))
    console.print()

    console.rule("mock Focus line — bar right-pinned")
    for count, rate in ((9, 1.0), (1234, 6.3), (99999, 9.8), (0, 0.0)):
        left = Text("Beacons: ")
        left.append(f"{count:,}", style="bold")
        bar = render_signal_bar(rate, width=10)
        pad = max(2, 30 - left.cell_len - bar.cell_len)
        row = Text(no_wrap=True)
        row.append_text(left)
        row.append(" " * pad)
        row.append_text(bar)
        console.print(row)


def animate(width: int = 16, fps: int = 30) -> None:
    """Live motion preview — drives the same easing + heartbeat the Focus view
    uses, off a simulated rate that wanders 0.3..9.5/s with a dead window every
    ~12 s. Ctrl+C to quit. (No hardware — pure simulation of the look.)"""
    from rich.live import Live

    t0 = time.time()
    display = 0.0
    try:
        with Live(console=console, refresh_per_second=fps, screen=False) as live:
            while True:
                t = time.time() - t0
                # 8 s alive (wandering), then a 4 s dead window for the heartbeat.
                if t % 12 < 8:
                    target = max(0.3, 5.5 + 4.0 * math.sin(t * 0.8))
                else:
                    target = 0.0

                if target <= 0.05:
                    pulse = 0.5 + 0.5 * math.sin(time.time() * math.tau)
                    bar = render_signal_bar(0.0, width=width, pulse=pulse)
                    display = 0.0
                else:
                    display += (target - display) * 0.25  # the 30 FPS ease
                    bar = render_signal_bar(display, width=width)

                row = Text(no_wrap=True)
                row.append(f"target {target:4.1f}/s   ", style="dim")
                row.append_text(bar)
                live.update(row)
                time.sleep(1 / fps)
    except KeyboardInterrupt:
        console.print()


if __name__ == "__main__":
    if "--animate" in sys.argv:
        animate()
    else:
        main()
