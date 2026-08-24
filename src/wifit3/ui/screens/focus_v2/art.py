"""Endpoint ANSI art + the green-LED breathe.

The ``.ans`` files are pre-rendered 24-bit art, 20x10 cells each. Convention:
any cell painted dark green ``rgb(0,128,0)`` is a live-indicator LED. The
breather lerps it toward bright green ``(0,255,0)`` and back on a slow cycle, so
the art self-describes what animates without coordinate tables in code (paint a
cell dark green and it breathes).
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
_GENERIC = "focus-card.ans"        # fallback card art when no per-card art resolves
_LED = (0, 128, 0)                 # dark green = the animation target

# Hybrid LED levels (green channel, 0-255). Idle is a *dim* breathe band so the
# art reads "alive but quiet"; a real packet punches a bright flicker spike well
# above that band, so activity is unmistakable against the idle glow.
_BREATHE_LO = 60
_BREATHE_HI = 150
_FLICKER_GREEN = 255


@lru_cache(maxsize=None)
def _load(name: str) -> Text:
    try:
        raw = (_ASSETS / name).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # A map entry pointing at a not-yet-drawn .ans must never crash the screen.
        raw = (_ASSETS / _GENERIC).read_text(encoding="utf-8")
    return Text.from_ansi(raw)


def art_size(name: str) -> tuple[int, int]:
    """(cell width, row count) of the art, used to pin the widget box."""
    lines = _load(name).split("\n")
    return max((len(ln.plain) for ln in lines), default=0), len(lines)


def _is_led(color: Color | None) -> bool:
    return color is not None and color.triplet is not None and tuple(color.triplet) == _LED


@lru_cache(maxsize=None)
def _transparent(name: str) -> Text:
    """Art with pure-black backgrounds made transparent."""
    return make_black_transparent(_load(name))


def _paint(name: str, green: int) -> Text:
    """The (transparent) art with its LED cells set to ``rgb(0, green, 0)``.
    Returns a fresh Text each call (copied from the cached ``_transparent``
    source). Textual takes ownership of the renderable, so a shared/cached
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

    The screen calls :meth:`pulse` on traffic: RX for the router, TX for the
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

    def set_art(self, name: str) -> None:
        """Swap which .ans this widget shows: the card art follows the selected/primary card. No-op
        when unchanged; re-pins the box (art may differ in size) and repaints now rather than
        waiting up to a frame for the next tick."""
        if name == self._name:
            return
        self._name = name
        w, h = art_size(name)
        self.styles.width = w
        self.styles.height = h
        self._repaint()


# --- Card art selection -----------------------------------------------------
_ART_BY_PRODUCT: dict[str, str] = {
    "ALFA AWUS036ACH": "cards/card-awus036ach.ans",
    "ALFA AWUS036ACS": "cards/card-awus036acs.ans",
    "ALFA AWUS036AXML": "cards/card-awus036axml.ans",
    "Panda PAU0F": "cards/card-pau0f.ans",
    "ALFA AWUS036AXML / Panda PAU0F": "cards/card-pau0f.ans",   # OUI unresolved -> default PAU0F
    "ALFA AWUS036H": "cards/card-awus036h.ans",
    "ALFA AWUS036NH": "cards/card-awus036nh.ans",
    "ALFA AWUS036NHA": "cards/card-awus036nha.ans",
    "ALFA AWUS036NHA / TL-WN722N v1": "cards/card-awus036nha.ans",   # OUI unresolved -> default ALFA
    "ALFA AWUS1900": "cards/card-awus1900.ans",
    "Archer T3U Plus": "cards/card-archert3uplus.ans",
    "TP-Link T2U Plus": "cards/card-archert2uplus.ans",
    "TP-Link T2U Nano": "cards/card-archert2unano.ans",
    "TP-Link Archer AC600 T2U Nano": "cards/card-archert2unano.ans",
    "Panda PAU05/06": "cards/card-pau06.ans",
    "Panda PAU0B": "cards/card-pau0b.ans",
    "Panda PAU09 N600": "cards/card-pau09n600.ans",
    "TL-WN722N v1": "cards/card-tpwn722nv23.ans",       # AR9271 sibling; same physical card, same art
    "TL-WN722N v2/v3": "cards/card-tpwn722nv23.ans",
    "Netgear A9000": "cards/card-netgeara9000.ans",
    "ASUS USB-BE93": "cards/card-asusbe93.ans",
}
_ART_BY_CHIPSET: dict[str, str] = {
    "RTL8821CU": "cards/card-auscomer600.ans",
    "RT2570": "cards/card-buffalonintendo.ans",
    "RT5370": "cards/card-lotekoo150.ans",
    "MT7610U": "cards/card-awus036achm.ans",
    "MT7612U": "cards/card-awus036acm.ans",
}


@lru_cache(maxsize=None)
def _exists(name: str) -> bool:
    return (_ASSETS / name).is_file()


def _card_art(iface) -> str | None:
    """The per-card .ans for one interface (product then chipset), or None if it has no art file."""
    name = getattr(getattr(iface, "driver", None), "product_name", None) or getattr(iface, "product_name", None)
    for cand in (_ART_BY_PRODUCT.get(name or ""), _ART_BY_CHIPSET.get(getattr(iface, "chipset", None) or "")):
        if cand and _exists(cand):
            return cand
    return None


def art_path_for(iface) -> str:
    """The .ans filename to show for a card: product_name -> chipset -> generic."""
    return _card_art(iface) or _GENERIC


def pick_primary(members):
    """The pool member whose art the card endpoint shows: the first with real per-card art, else the
    first member (order doesn't matter for the pick, only that one is chosen)."""
    for iface in members:
        if _card_art(iface):
            return iface
    return members[0] if members else None


def pool_art(members) -> str:
    """The .ans for the whole pool: the primary's art, or the generic when the pool is empty."""
    primary = pick_primary(members)
    return art_path_for(primary) if primary is not None else _GENERIC


def display_name(iface) -> str:
    """One card's human name: its driver-refined product_name (mt7921au OUI split), else the static
    product_name, else the bare chipset. ``"card"`` when nothing is set."""
    return (getattr(getattr(iface, "driver", None), "product_name", None)
            or getattr(iface, "product_name", None)
            or getattr(iface, "chipset", None) or "card")
