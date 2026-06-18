"""Shared ANSI-art helpers — load ``.ans`` art so it blends into the theme.

The ``.ans`` files (textual-paint output) bake a pure-black ``(0,0,0)`` canvas
into every cell. ``make_black_transparent`` drops that black so the art inherits
the widget's background instead of painting a hard rectangle. Used by the Splash
logo and the Focus v2 endpoint art.
"""
from __future__ import annotations

from rich.color import Color
from rich.console import Console
from rich.style import Style
from rich.text import Text

_BLACK = (0, 0, 0)
_CONSOLE = Console()          # only for get_style_at_offset; never writes output


def is_black(color: Color | None) -> bool:
    return color is not None and color.triplet is not None and tuple(color.triplet) == _BLACK


def _without_bgcolor(style: Style) -> Style:
    """``style`` with the background unset (preserving fg + text attributes)."""
    return Style(
        color=style.color,
        bold=style.bold, dim=style.dim, italic=style.italic,
        underline=style.underline, blink=style.blink, blink2=style.blink2,
        reverse=style.reverse, conceal=style.conceal, strike=style.strike,
        underline2=style.underline2, frame=style.frame,
        encircle=style.encircle, overline=style.overline, link=style.link,
    )


def make_black_transparent(text: Text, *, blank_black_ink: bool = False) -> Text:
    """Drop pure-black backgrounds so ``text`` inherits the theme surface.

    With ``blank_black_ink`` also blank black-foreground glyphs — for art that
    draws *with* black ink, not just on a black canvas (the Focus endpoint art).
    Without it, black ink is kept (the Splash logo draws in colour on a black
    canvas, so only the canvas is dropped)."""
    if not blank_black_ink:
        out = text.copy()
        out.spans = [
            span._replace(style=_without_bgcolor(span.style))
            if isinstance(span.style, Style) and is_black(span.style.bgcolor) else span
            for span in out.spans
        ]
        return out

    out = Text()
    for i, ch in enumerate(text.plain):
        if ch == "\n":
            out.append("\n")
            continue
        st = text.get_style_at_offset(_CONSOLE, i)
        fg_black = is_black(st.color)
        out.append(
            " " if fg_black else ch,
            Style(color=None if fg_black else st.color,
                  bgcolor=None if is_black(st.bgcolor) else st.bgcolor),
        )
    return out
