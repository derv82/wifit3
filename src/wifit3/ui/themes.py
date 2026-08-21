"""Textual theme loading for Wifit3.

Built-in Wifit3 themes live as TOML files in ``ui/themes/``; user themes live in
``<platform config dir>/themes/``. See ``docs/THEMES.md`` for the schema. Invalid user themes are
skipped and logged; they must never break app startup.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import tomllib
from typing import Any

from platformdirs import user_config_dir
from rich.color import Color, ColorParseError
from textual.theme import Theme

logger = logging.getLogger(__name__)

BUILTIN_THEMES_DIR = Path(__file__).with_name("themes")
USER_THEMES_DIR = Path(user_config_dir("wifit3", appauthor=False)) / "themes"
_SCHEMA = 1
_DEFAULT_PRIMARY = "#00ff88"
_COLOR_KEYS = {
    "primary", "secondary", "warning", "error", "success", "accent", "foreground",
    "background", "surface", "panel", "boost",
}
# Removes matching registered themes from Wifit3's available theme list.
BLACKLISTED_THEME_NAMES: list[str] = []


@dataclass(frozen=True)
class ThemeReloadResult:
    changed: bool
    registered: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()


class ThemeFilePoller:
    def __init__(self, *, user_dir: Path | None = None):
        self.user_dir = user_dir or USER_THEMES_DIR
        self._snapshot: dict[Path, int | None] = {}

    def start(self) -> None:
        self._snapshot = self._scan()

    def reload_if_changed(self, app) -> ThemeReloadResult:
        snapshot = self._scan()
        if snapshot == self._snapshot:
            return ThemeReloadResult(changed=False)
        self._snapshot = snapshot
        return register_app_themes(app, user_dir=self.user_dir)

    def _scan(self) -> dict[Path, int | None]:
        paths = [*_theme_files(BUILTIN_THEMES_DIR), *_theme_files(self.user_dir)]
        return {path: _mtime_ns(path) for path in paths}


def register_app_themes(app, *, user_dir: Path | None = None) -> ThemeReloadResult:
    """Register packaged Wifit3 themes, then user-defined themes.

    User themes are registered second so a user may intentionally override a Wifit3 theme name.
    Built-in Textual themes are owned by Textual. Hardcoded Wifit3-blacklisted themes are
    removed after registration.
    """
    registered: list[str] = []
    skipped: list[str] = []
    for path in _theme_files(BUILTIN_THEMES_DIR):
        _register_theme_file(app, path, registered, skipped)
    for path in _theme_files(user_dir or USER_THEMES_DIR):
        _register_theme_file(app, path, registered, skipped)
    unregister_blacklisted_themes(app)
    return ThemeReloadResult(
        changed=bool(registered or skipped),
        registered=tuple(registered), skipped=tuple(skipped),
    )


def is_theme_blacklisted(name: str) -> bool:
    return name in BLACKLISTED_THEME_NAMES


def unregister_blacklisted_themes(app) -> None:
    themes = getattr(app, "_registered_themes", None)
    if themes is None:
        themes = getattr(app, "available_themes", None)
    if themes is None:
        return
    for name in BLACKLISTED_THEME_NAMES:
        themes.pop(name, None)


def _theme_files(path: Path) -> list[Path]:
    try:
        return sorted(p for p in path.glob("*.toml") if p.is_file())
    except OSError as exc:
        logger.warning("Failed to scan theme directory %s: %s", path, exc)
        return []


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _register_theme_file(
    app,
    path: Path,
    registered: list[str] | None = None,
    skipped: list[str] | None = None,
) -> None:
    try:
        theme = load_theme_file(path)
        app.register_theme(theme)
        if registered is not None:
            registered.append(theme.name)
    except ThemeLoadError as exc:
        logger.warning("Skipping theme %s: %s", path, exc)
        if skipped is not None:
            skipped.append(path.name)


class ThemeLoadError(Exception):
    pass


def load_theme_file(path: Path) -> Theme:
    """Load one schema-v1 TOML theme file into a Textual ``Theme``."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ThemeLoadError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ThemeLoadError("theme file must be a TOML table")
    schema = data.get("schema", _SCHEMA)
    if schema != _SCHEMA:
        raise ThemeLoadError(f"unsupported schema {schema!r}")
    theme = data.get("theme")
    colors = data.get("colors", {})
    variables = data.get("variables", {})
    if not isinstance(theme, dict):
        raise ThemeLoadError("missing [theme] table")
    if not isinstance(colors, dict):
        raise ThemeLoadError("[colors] must be a table")
    if not isinstance(variables, dict):
        raise ThemeLoadError("[variables] must be a table")

    name = _string(theme.get("name"))
    if name is None:
        raise ThemeLoadError("[theme].name is required")
    dark = theme.get("dark", True)
    if not isinstance(dark, bool):
        raise ThemeLoadError("[theme].dark must be true or false")

    kwargs: dict[str, Any] = {"name": name, "dark": dark}
    for key in _COLOR_KEYS:
        raw = colors.get(key)
        if raw is None and key == "primary":
            raw = _DEFAULT_PRIMARY
        if raw is None:
            continue
        color = _color(raw, f"colors.{key}")
        kwargs[key] = color
    kwargs["variables"] = {
        str(key): _color(value, f"variables.{key}")
        for key, value in variables.items()
    }
    return Theme(**kwargs)


def _string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _color(value: object, key: str) -> str:
    color = _string(value)
    if color is None:
        raise ThemeLoadError(f"{key} must be a color string")
    color = _normalize_hex_color(color)
    try:
        Color.parse(color)
    except ColorParseError as exc:
        raise ThemeLoadError(f"{key} is not a valid color: {color}") from exc
    return color


def _normalize_hex_color(color: str) -> str:
    if not color.startswith("#"):
        return color
    raw = color[1:]
    if not raw or any(ch not in "0123456789abcdefABCDEF" for ch in raw):
        return color
    if len(raw) == 1:
        return f"#{raw * 6}"
    if len(raw) == 2:
        return f"#{raw * 3}"
    if len(raw) in (3, 4):
        return "#" + "".join(ch * 2 for ch in raw[:3])
    if len(raw) == 8:
        return f"#{raw[:6]}"
    return color
