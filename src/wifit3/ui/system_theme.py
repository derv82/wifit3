"""System light/dark preference detection."""
from __future__ import annotations

import subprocess
import sys


SYSTEM_THEME = "system"
SYSTEM_DARK_THEME = "wifit3-green-dark"
SYSTEM_LIGHT_THEME = "textual-light"


def system_appearance() -> str | None:
    """Return ``dark`` or ``light`` when the system preference is available."""
    if sys.platform == "win32":
        return _windows_appearance()
    if sys.platform == "darwin":
        return _macos_appearance()
    if sys.platform == "linux":
        return _linux_appearance()
    return None


def system_appearance_label() -> str:
    """Human label for the available system preference, or ``Unavailable``."""
    appearance = system_appearance()
    return appearance.title() if appearance else "Unavailable"


def preferred_theme(
    dark_theme: str = SYSTEM_DARK_THEME, light_theme: str = SYSTEM_LIGHT_THEME
) -> str:
    """Return the configured theme for the current system light/dark preference."""
    return light_theme if system_appearance() == "light" else dark_theme


def _windows_appearance() -> str | None:
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except (ImportError, OSError):
        return None
    return {0: "dark", 1: "light"}.get(value)


def _macos_appearance() -> str | None:
    try:
        result = subprocess.run(
            ("defaults", "read", "-g", "AppleInterfaceStyle"),
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode not in (0, 1):
        return None
    return "dark" if "dark" in result.stdout.lower() else "light"


def _linux_appearance() -> str | None:
    for key in ("color-scheme", "gtk-theme"):
        try:
            result = subprocess.run(
                ("gsettings", "get", "org.gnome.desktop.interface", key),
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        value = result.stdout.strip().strip("'")
        if result.returncode == 0 and value:
            return "dark" if "dark" in value.lower() else "light"
    return None
