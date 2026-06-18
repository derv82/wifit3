"""Endpoint ANSI art + the green-LED breathe.

The ``.ans`` files are pre-rendered 24-bit art, 20x10 cells each. Convention:
any cell painted dark green ``rgb(0,128,0)`` is a live-indicator LED — the
breather lerps it toward bright green ``(0,255,0)`` and back on a slow cycle, so
the art self-describes what animates without coordinate tables in code (paint a
cell dark green and it breathes). See ``planning/FOCUS-REDESIGN.md`` →
"Green-LED breathe convention".
"""
from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

from rich.color import Color
from rich.style import Style
from rich.text import Span, Text
from textual.widgets import Static

_ASSETS = Path(__file__).parent.parent.parent / "assets"
_LED = (0, 128, 0)                 # dark green = the animation target


@lru_cache(maxsize=None)
def _load(name: str) -> Text:
    return Text.from_ansi((_ASSETS / name).read_text(encoding="utf-8"))


def art_size(name: str) -> tuple[int, int]:
    """(cell width, row count) of the art — used to pin the widget box."""
    lines = _load(name).split("\n")
    return max((len(ln.plain) for ln in lines), default=0), len(lines)


def _is_led(color: Color | None) -> bool:
    return color is not None and color.triplet is not None and tuple(color.triplet) == _LED


def breathe(name: str, phase: float) -> Text:
    """The art with its LED cells lerped to a brightness set by ``phase`` (0..1,
    one smooth dark->bright->dark cycle). Non-LED cells are untouched."""
    factor = (1.0 - math.cos(2.0 * math.pi * phase)) / 2.0      # 0 -> 1 -> 0
    lit = Color.from_rgb(0, int(round(128 + 127 * factor)), 0)
    src = _load(name)
    spans: list[Span] = []
    for span in src.spans:
        st = span.style
        if isinstance(st, Style) and (_is_led(st.color) or _is_led(st.bgcolor)):
            patch = {}
            if _is_led(st.color):
                patch["color"] = lit
            if _is_led(st.bgcolor):
                patch["bgcolor"] = lit
            st = st + Style(**patch)
        spans.append(Span(span.start, span.end, st))
    out = src.copy()
    out.spans = spans
    return out


class BreathingArt(Static):
    """A fixed-size art widget whose green LED cells breathe. ~1.5 s cycle at
    10 FPS; throttle the interval for SSH/pihole later (animation-as-
    instrumentation, so the rate is the only knob)."""

    CYCLE_S = 1.5
    FPS = 10

    def __init__(self, art_name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._name = art_name
        self._phase = 0.0

    def on_mount(self) -> None:
        w, h = art_size(self._name)
        self.styles.width = w
        self.styles.height = h
        self.update(breathe(self._name, 0.0))
        self.set_interval(1.0 / self.FPS, self._tick)

    def _tick(self) -> None:
        self._phase = (self._phase + 1.0 / (self.FPS * self.CYCLE_S)) % 1.0
        self.update(breathe(self._name, self._phase))
