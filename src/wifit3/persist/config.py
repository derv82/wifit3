"""Persistent user preferences: a flat TOML file in the OS config dir."""
from __future__ import annotations

import tomllib
from pathlib import Path

from platformdirs import user_config_dir

_PATH = Path(user_config_dir("wifit3", appauthor=False)) / "config.toml"


class ConfigError(Exception):
    pass


class Config:
    theme: str = "textual-dark"
    scanner_sort: str = "signal"
    scanner_sort_reverse: bool = True
    silenced_bssids: list[str] = []

    @classmethod
    def is_silenced(cls, bssid: str) -> bool:
        return bssid.lower() in cls.silenced_bssids

    @classmethod
    def load(cls) -> None:
        try:
            data = tomllib.loads(_PATH.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"Failed to load config at {_PATH}: {e}") from e
        cls.theme = data.get("theme", cls.theme)
        cls.scanner_sort = data.get("scanner_sort", cls.scanner_sort)
        cls.scanner_sort_reverse = data.get("scanner_sort_reverse", cls.scanner_sort_reverse)
        raw = data.get("silenced_bssids", cls.silenced_bssids)
        cls.silenced_bssids = [str(x).lower() for x in raw] if isinstance(raw, list) else cls.silenced_bssids

    @classmethod
    def save(cls) -> None:
        text = (
            f"theme = {_fmt(cls.theme)}\n"
            f"scanner_sort = {_fmt(cls.scanner_sort)}\n"
            f"scanner_sort_reverse = {_fmt(cls.scanner_sort_reverse)}\n"
            f"silenced_bssids = {_fmt(cls.silenced_bssids)}\n"
        )
        try:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            _PATH.write_text(text, encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"Failed to save config at {_PATH}: {e}") from e


def _fmt(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    return "'" + str(v) + "'"
