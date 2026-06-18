"""The packet-flow channel — the centerpiece.

A multi-row sparkline meter the v1 ``PacketDashboard``'s single-series paint
can't express: N labelled rows, per-row colour, a trailing ``/s`` rate (or a
recent count), all flowing **right->left** (newest sample at the right edge,
history scrolling left — you read the attack's recent past L->R). Height is
**adaptive**: 2-row (16 levels) when there's vertical room, 1-row (8 levels)
when cramped — the same "shrink gracefully" rule the bottom band rides.

The shell feeds lively fake data so the look can be judged; real wiring samples
``WlanInterface.packet_stats`` deltas into ``_hist`` later (the render is
data-source agnostic).
"""
from __future__ import annotations

import math
from collections import deque

from rich.text import Text
from textual.widgets import Static

# 0..8 eighths; index 0 is blank (a quiet column reads as empty, not a bar).
_BLOCKS = " ▁▂▃▄▅▆▇█"
_LABEL_W = 6
_NUM_W = 5
_GUTTER = _LABEL_W + 1 + 1 + _NUM_W      # label + space + space + number
_HISTORY = 256


class FlowChannel(Static):
    SAMPLE_S = 0.4                        # fake-sample cadence; ~window for the rate

    def __init__(self, rows, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rows = rows                 # list[FlowRow]
        self._hist = {r.key: deque([0] * _HISTORY, maxlen=_HISTORY) for r in rows}
        self._t = 0

    def on_mount(self) -> None:
        self.set_interval(self.SAMPLE_S, self._tick)
        self._tick()

    def on_resize(self) -> None:
        self._repaint()

    # ---- fake data (shell only) --------------------------------------------

    def _tick(self) -> None:
        self._t += 1
        for r in self._rows:
            # Per-window counts scaled so the trailing /s reads near the row's
            # nominal peak; a periodic burst makes inject/deauth/eapol pulse.
            wobble = 0.8 + 0.4 * math.sin(self._t / 4.0 + (hash(r.key) % 7))
            sample = r.peak * self.SAMPLE_S * wobble
            if r.key in ("inject", "deauth", "eapol") and (self._t % 11) in (0, 1):
                sample += r.peak * self.SAMPLE_S * 0.8
            self._hist[r.key].append(max(0, int(round(sample))))
        self._repaint()

    # ---- paint --------------------------------------------------------------

    def _bar_width(self) -> int:
        return max(4, (self.content_size.width or 50) - _GUTTER)

    def _two_row(self) -> bool:
        h = self.content_size.height or 12
        return h // max(1, len(self._rows)) >= 2

    def _col(self, v: int, peak: int, levels: int) -> int:
        if v <= 0:
            return 0
        return max(1, min(levels, round(v / peak * levels)))

    def _repaint(self) -> None:
        bw = self._bar_width()
        two = self._two_row()
        lines: list[Text] = []
        for r in self._rows:
            window = list(self._hist[r.key])[-bw:]
            peak = max(max(window, default=0), 1)
            recent = list(self._hist[r.key])[-6:]
            avg = sum(recent) / (len(recent) * self.SAMPLE_S) if recent else 0
            num = f"{avg:.0f}/s" if r.as_rate else str(sum(recent))

            if two:
                upper = "".join(_BLOCKS[max(0, self._col(v, peak, 16) - 8)] for v in window)
                lower = "".join(_BLOCKS[min(8, self._col(v, peak, 16))] for v in window)
                lines.append(self._row("", r.color, upper, "", bw))
                lines.append(self._row(r.label, r.color, lower, num, bw))
            else:
                cells = "".join(_BLOCKS[self._col(v, peak, 8)] for v in window)
                lines.append(self._row(r.label, r.color, cells, num, bw))
        # Vertically centre the sparkline block so it lines up with the
        # vertically-centred card/router columns as the band grows.
        h = self.content_size.height or len(lines)
        pad = max(0, (h - len(lines)) // 2)
        self.update(Text("\n").join([Text("")] * pad + lines))

    def _row(self, label: str, color: str, cells: str, num: str, bw: int) -> Text:
        """Right-aligned label · space · bars (right-aligned, newest at the right)
        · space · left-aligned number — label and number sit flush against the
        bars. Blank label/number on the upper row of a 2-row pair."""
        t = Text()
        t.append(f"{label:>{_LABEL_W}} ", style=color if label else "")
        t.append(cells.rjust(bw), style=color)
        t.append(" ")
        t.append(f"{num:<{_NUM_W}}", style=color if num else "")
        return t
