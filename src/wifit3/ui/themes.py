"""Wifit3's custom Textual themes."""
from textual.theme import Theme

from wifit3.ui.ansi_art import (
    THEME_BARS_PRIMARY_KEY, THEME_BARS_SECONDARY_KEY,
    THEME_TEXT_PRIMARY_KEY, THEME_TEXT_SECONDARY_KEY
)

def register_app_themes(app) -> None:
    for theme in custom_themes():
        app.register_theme(theme)


def custom_themes() -> list[Theme]:
    return [
        _wifit3_green_dark(),
    ]


def _wifit3_green_dark() -> Theme:
    return Theme(
        name="wifit3-green-dark",
        dark=True,
        primary="#00ff88",
        secondary="#00c8ff",
        accent="#00ff88",
        foreground="#d8ffe8",
        background="#050805",
        success="#00ff88",
        warning="#ffd75f",
        error="#cc6666",
        surface="#0b120b",
        panel="#101810",
        variables={
            "block-cursor-background": "#00ff88",
            "block-cursor-blurred-background": "#1f5f3f",
            "block-hover-background": "#163322",
            "input-selection-background": "#005f3a",
            "screen-selection-background": "#007a48",
            # Splash logo colors
            THEME_BARS_PRIMARY_KEY: "#00ff22",
            THEME_BARS_SECONDARY_KEY: "#008f22",
            THEME_TEXT_PRIMARY_KEY: "#f4fff8",
            THEME_TEXT_SECONDARY_KEY: "#7aa88a",
        },
    )
