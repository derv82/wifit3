"""Shared ANSI-art helpers: load ``.ans`` art."""
from __future__ import annotations

from rich.color import Color
from rich.style import Style
from rich.text import Text

# Textual style variable names
THEME_BARS_PRIMARY_KEY   = "wifit3-logo-bars-primary"
THEME_BARS_SECONDARY_KEY = "wifit3-logo-bars-secondary"
THEME_TEXT_PRIMARY_KEY   = "wifit3-logo-text-primary"
THEME_TEXT_SECONDARY_KEY = "wifit3-logo-text-secondary"

_LOGO_COLOR_MAP = {
    (  0, 255,   0): THEME_BARS_PRIMARY_KEY,
    (  0, 128,   0): THEME_BARS_SECONDARY_KEY,
    (255, 255, 255): THEME_TEXT_PRIMARY_KEY,
    (128, 128, 128): THEME_TEXT_SECONDARY_KEY,
}
_LOGO_DARK_DEFAULTS = {
    THEME_BARS_PRIMARY_KEY:   "#00ff00",
    THEME_BARS_SECONDARY_KEY: "#008000",
    THEME_TEXT_PRIMARY_KEY:   "#ffffff",
    THEME_TEXT_SECONDARY_KEY: "#808080",
}
_LOGO_LIGHT_DEFAULTS = {
    THEME_BARS_PRIMARY_KEY:   "#00bb00",
    THEME_BARS_SECONDARY_KEY: "#008000",
    THEME_TEXT_PRIMARY_KEY:   "#111111",
    THEME_TEXT_SECONDARY_KEY: "#666666",
}


def is_black(color: Color | None) -> bool:
    if color is None or color.triplet is None:
        return False
    return color.triplet == (0, 0, 0)


def _without_bgcolor(style: Style) -> Style:
    """``style`` with the background unset (preserving fg + text attributes)."""
    return style.without_color + Style(color=style.color)


def _mapped_logo_color(color: Color | None, palette: dict[str, str]) -> str | None:
    if not color or not color.triplet:
        return None
    key = _LOGO_COLOR_MAP.get(tuple(color.triplet))
    if key is None:
        return None
    return palette.get(key)


def recolor_logo(text: Text, variables: dict[str, str], *, dark: bool = True) -> Text:
    """Replace the logo's baked ANSI palette with theme logo variables."""
    defaults = _LOGO_DARK_DEFAULTS if dark else _LOGO_LIGHT_DEFAULTS
    palette = {**defaults, **variables}  # variables win
    out = text.copy()
    spans = []
    for span in out.spans:
        fg = _mapped_logo_color(span.style.color, palette)
        bg = _mapped_logo_color(span.style.bgcolor, palette)
        style = span.style + Style(color=fg, bgcolor=bg)
        spans.append(span._replace(style=style))
    out.spans = spans
    return out


def make_black_transparent(text: Text) -> Text:
    """Drop pure-black backgrounds so ``text`` inherits the theme surface."""
    out = text.copy()
    out.spans = [
        span._replace(style=_without_bgcolor(span.style))
        if is_black(span.style.bgcolor) else span
        for span in out.spans
    ]
    return out
