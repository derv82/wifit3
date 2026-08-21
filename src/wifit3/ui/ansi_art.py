"""Shared ANSI-art helpers: load ``.ans`` art so it blends into the theme.

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
_LOGO_COLOR_MAP = {
    (0, 255, 0): "logo_color_primary",
    (0, 128, 0): "logo_color_secondary",
    (255, 255, 255): "logo_text_primary",
    (128, 128, 128): "logo_text_secondary",
}
_LOGO_DARK_DEFAULTS = {
    "logo_color_primary": "#00ff00",
    "logo_color_secondary": "#008000",
    "logo_text_primary": "#ffffff",
    "logo_text_secondary": "#808080",
}
_LOGO_LIGHT_DEFAULTS = {
    "logo_color_primary": "#00bb00",
    "logo_color_secondary": "#008000",
    "logo_text_primary": "#111111",
    "logo_text_secondary": "#666666",
}
_CONSOLE = Console()          # only for get_style_at_offset; never writes output


def is_black(color: Color | None) -> bool:
    return color is not None and color.triplet is not None and tuple(color.triplet) == _BLACK


def _style_like(style: Style, *, color: Color | str | None, bgcolor: Color | str | None) -> Style:
    return Style(
        color=color, bgcolor=bgcolor,
        bold=style.bold, dim=style.dim, italic=style.italic,
        underline=style.underline, blink=style.blink, blink2=style.blink2,
        reverse=style.reverse, conceal=style.conceal, strike=style.strike,
        underline2=style.underline2, frame=style.frame,
        encircle=style.encircle, overline=style.overline, link=style.link,
    )


def _without_bgcolor(style: Style) -> Style:
    """``style`` with the background unset (preserving fg + text attributes)."""
    return _style_like(style, color=style.color, bgcolor=None)


def _mapped_logo_color(
    color: Color | None,
    variables: dict[str, str],
    defaults: dict[str, str],
) -> Color | str | None:
    if color is None or color.triplet is None:
        return color
    key = _LOGO_COLOR_MAP.get(tuple(color.triplet))
    return variables.get(key, defaults.get(key, color)) if key else color


def recolor_logo(text: Text, variables: dict[str, str], *, dark: bool = True) -> Text:
    """Replace the logo's baked ANSI palette with theme logo variables."""
    defaults = _LOGO_DARK_DEFAULTS if dark else _LOGO_LIGHT_DEFAULTS
    out = text.copy()
    spans = []
    for span in out.spans:
        style = Style.parse(span.style) if isinstance(span.style, str) else span.style
        if not isinstance(style, Style):
            spans.append(span)
            continue
        spans.append(span._replace(style=_style_like(
            style,
            color=_mapped_logo_color(style.color, variables, defaults),
            bgcolor=_mapped_logo_color(style.bgcolor, variables, defaults),
        )))
    out.spans = spans
    return out


def make_black_transparent(text: Text, *, blank_black_ink: bool = False) -> Text:
    """Drop pure-black backgrounds so ``text`` inherits the theme surface."""
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
