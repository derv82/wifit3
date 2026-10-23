"""System light/dark preference detection."""
from __future__ import annotations

import os
import re
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
    return _first_appearance(_xdg_appearance(), _gtk_appearance(), _xsettings_appearance(),
                             _terminal_appearance())


def _xdg_appearance() -> str | None:
    for command in (
        (
            "gdbus", "call", "--session", "--dest", "org.freedesktop.portal.Desktop",
            "--object-path", "/org/freedesktop/portal/desktop",
            "--method", "org.freedesktop.portal.Settings.Read",
            "org.freedesktop.appearance", "color-scheme",
        ),
        (
            "busctl", "--user", "call", "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop", "org.freedesktop.portal.Settings",
            "Read", "ss", "org.freedesktop.appearance", "color-scheme",
        ),
    ):
        output = _command_output(command)
        if output is None:
            continue
        match = re.search(r"\b(?:uint32|u)\s+([012])\b", output)
        if match is not None:
            return {"1": "dark", "2": "light"}.get(match.group(1))
    return None


def _gtk_appearance() -> str | None:
    color_scheme = _command_output(
        ("gsettings", "get", "org.gnome.desktop.interface", "color-scheme")
    )
    if color_scheme and "default" not in color_scheme.lower():
        return "dark" if "dark" in color_scheme.lower() else "light"
    gtk_theme = _command_output(("gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"))
    if gtk_theme:
        return "dark" if "dark" in gtk_theme.lower() else "light"
    return None


def _xsettings_appearance() -> str | None:
    theme = _command_output(("xfconf-query", "-c", "xsettings", "-p", "/Net/ThemeName"))
    if theme is None:
        return None
    return "dark" if "dark" in theme.lower() else "light"


def _terminal_appearance() -> str | None:
    fields = re.split(r"[;:]", os.environ.get("COLORFGBG", ""))
    for field in reversed(fields):
        try:
            color = int(field)
        except ValueError:
            continue
        if 0 <= color <= 15:
            return "dark" if color < 8 else "light"
    return None


def _command_output(command: tuple[str, ...], *, allow_status: tuple[int, ...] = (0,)) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=1, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = result.stdout.strip()
    return output if result.returncode in allow_status and output else None


def _first_appearance(*appearances: str | None) -> str | None:
    return next((appearance for appearance in appearances if appearance is not None), None)
