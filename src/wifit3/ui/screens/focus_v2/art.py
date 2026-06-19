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

from ...ansi_art import make_black_transparent

_ASSETS = Path(__file__).parent.parent.parent / "assets"
_LED = (0, 128, 0)                 # dark green = the animation target

# Hybrid LED levels (green channel, 0-255). Idle is a *dim* breathe band so the
# art reads "alive but quiet"; a real packet punches a bright flicker spike well
# above that band, so activity is unmistakable against the idle glow.
_BREATHE_LO = 60
_BREATHE_HI = 150
_FLICKER_GREEN = 255


@lru_cache(maxsize=None)
def _load(name: str) -> Text:
    return Text.from_ansi((_ASSETS / name).read_text(encoding="utf-8"))


def art_size(name: str) -> tuple[int, int]:
    """(cell width, row count) of the art — used to pin the widget box."""
    lines = _load(name).split("\n")
    return max((len(ln.plain) for ln in lines), default=0), len(lines)


def _is_led(color: Color | None) -> bool:
    return color is not None and color.triplet is not None and tuple(color.triplet) == _LED


@lru_cache(maxsize=None)
def _transparent(name: str) -> Text:
    """Art with pure-black cells made transparent (both the black canvas and the
    black ink), so it blends into the theme surface. See ``ui/ansi_art``."""
    return make_black_transparent(_load(name), blank_black_ink=True)


def _paint(name: str, green: int) -> Text:
    """The (transparent) art with its LED cells set to ``rgb(0, green, 0)``.
    Returns a fresh Text each call (copied from the cached ``_transparent``
    source) — Textual takes ownership of the renderable, so a shared/cached
    instance must not be handed to ``update()``."""
    lit = Color.from_rgb(0, green, 0)
    src = _transparent(name)
    spans: list[Span] = []
    for span in src.spans:
        st = span.style
        if isinstance(st, Style) and _is_led(st.color):
            st = st + Style(color=lit)
        spans.append(Span(span.start, span.end, st))
    out = src.copy()
    out.spans = spans
    return out


def _breathe_green(phase: float) -> int:
    """Idle-glow green level for ``phase`` (0..1, one smooth lo->hi->lo cycle)."""
    factor = (1.0 - math.cos(2.0 * math.pi * phase)) / 2.0      # 0 -> 1 -> 0
    return int(round(_BREATHE_LO + (_BREATHE_HI - _BREATHE_LO) * factor))


def breathe(name: str, phase: float) -> Text:
    """The art with its LED cells at the idle-breathe level for ``phase``. The
    flicker spike rides on top of this in :class:`BreathingArt`."""
    return _paint(name, _breathe_green(phase))


class BreathingArt(Static):
    """Endpoint art whose green LED cells do a dim idle *breathe*, with a bright
    *flicker* spike on each real packet (instrumentation, not decoration).

    The screen calls :meth:`pulse` on traffic — RX for the router, TX for the
    card. Breathe (~1.5 s cycle) keeps the art alive while idle so it never looks
    dead; the flicker is throttled by a 1-frame ON + 2-frame refractory state
    machine (~3.3 Hz cap at 10 FPS), so a beacon storm or 400 Hz WEP injection
    blinks at a calm rate instead of pinning the LED solid-on or strobing."""

    CYCLE_S = 1.5
    FPS = 10
    _ON_FRAMES = 1            # flicker bright for ~0.1 s …
    _REFRACTORY_FRAMES = 2    # … then forced dim ~0.2 s, ignoring fresh pulses

    def __init__(self, art_name: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._name = art_name
        self._phase = 0.0
        self._blink = "idle"      # idle | on | refractory
        self._blink_left = 0
        self._pending = False     # a pulse arrived mid-cycle → blink again next

    def on_mount(self) -> None:
        w, h = art_size(self._name)
        self.styles.width = w
        self.styles.height = h
        self._repaint()
        self.set_interval(1.0 / self.FPS, self._tick)

    def pulse(self) -> None:
        """Signal a packet. Starts a flicker when idle; during an in-progress
        flicker/refractory it just arms the next one, so a continuous stream
        blinks at the capped rate rather than holding the LED solid-on."""
        if self._blink == "idle":
            self._blink, self._blink_left = "on", self._ON_FRAMES
        else:
            self._pending = True

    def _tick(self) -> None:
        self._phase = (self._phase + 1.0 / (self.FPS * self.CYCLE_S)) % 1.0
        self._repaint()           # render the current state for its full duration
        self._advance_blink()     # …then step the flicker machine for next frame

    def _advance_blink(self) -> None:
        if self._blink == "idle":
            return
        self._blink_left -= 1
        if self._blink_left > 0:
            return
        if self._blink == "on":
            self._blink, self._blink_left = "refractory", self._REFRACTORY_FRAMES
        elif self._pending:
            self._blink, self._blink_left, self._pending = "on", self._ON_FRAMES, False
        else:
            self._blink = "idle"

    def _repaint(self) -> None:
        green = _FLICKER_GREEN if self._blink == "on" else _breathe_green(self._phase)
        self.update(_paint(self._name, green))
