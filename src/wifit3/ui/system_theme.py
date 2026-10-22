"""System light/dark preference detection."""
from __future__ import annotations

import darkdetect


SYSTEM_THEME = "system"
SYSTEM_DARK_THEME = "wifit3-green-dark"
SYSTEM_LIGHT_THEME = "textual-light"


def preferred_theme(
    dark_theme: str = SYSTEM_DARK_THEME, light_theme: str = SYSTEM_LIGHT_THEME
) -> str:
    """Return the configured theme for the current system light/dark preference."""
    try:
        return light_theme if darkdetect.isDark() is False else dark_theme
    except Exception:
        return dark_theme
