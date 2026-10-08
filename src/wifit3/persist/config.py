"""Persistent user preferences in the OS config directory."""
from __future__ import annotations

import tomllib
from pathlib import Path

from platformdirs import user_config_dir

_PATH = Path(user_config_dir("wifit3", appauthor=False)) / "config.toml"


class ConfigError(Exception):
    pass


class Config:
    theme: str = "textual-dark"
    captures_dir: str = "captures"
    save_pcap: bool = True
    log_level: str = "info"
    scanner_sort: str = "signal"
    scanner_sort_reverse: bool = True
    scanner_sort_delay: float = 2.0
    silenced_bssids: list[str] = []
    decloaked_ssids: dict[str, str] = {}
    hide_silenced: bool = False
    hashcat_path: str | None = None
    wordlist_path: str | None = None

    @classmethod
    def is_silenced(cls, bssid: str) -> bool:
        return bssid.lower() in cls.silenced_bssids

    @classmethod
    def decloaked_ssid(cls, bssid: str) -> str | None:
        return cls.decloaked_ssids.get(bssid.lower())

    @classmethod
    def remember_decloak(cls, bssid: str, ssid: str) -> bool:
        """Remember a confirmed BSSID-to-SSID mapping. Returns whether it changed."""
        if not ssid or len(ssid.encode("utf-8")) > 32:
            return False
        key = bssid.lower()
        if cls.decloaked_ssids.get(key) == ssid:
            return False
        cls.decloaked_ssids[key] = ssid
        return True

    @classmethod
    def load(cls) -> None:
        try:
            data = tomllib.loads(_PATH.read_text("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise ConfigError(f"Failed to load config at {_PATH}: {e}") from e
        cls.captures_dir = data.get("captures_dir", cls.captures_dir)
        cls.save_pcap = data.get("save_pcap", cls.save_pcap)
        cls.theme = data.get("theme", cls.theme)
        cls.log_level = data.get("log_level", cls.log_level)
        cls.scanner_sort = data.get("scanner_sort", cls.scanner_sort)
        cls.scanner_sort_reverse = data.get("scanner_sort_reverse", cls.scanner_sort_reverse)
        raw_delay = data.get("scanner_sort_delay", cls.scanner_sort_delay)
        try:
            cls.scanner_sort_delay = float(raw_delay)
        except (ValueError, TypeError):
            pass
        raw = data.get("silenced_bssids", cls.silenced_bssids)
        cls.silenced_bssids = [str(x).lower() for x in raw] if isinstance(raw, list) else cls.silenced_bssids
        raw_decloaks = data.get("decloak", cls.decloaked_ssids)
        if isinstance(raw_decloaks, dict):
            cls.decloaked_ssids = {
                str(bssid).lower(): ssid
                for bssid, ssid in raw_decloaks.items()
                if isinstance(ssid, str) and ssid and len(ssid.encode("utf-8")) <= 32
            }
        cls.hide_silenced = bool(data.get("hide_silenced", cls.hide_silenced))
        cls.hashcat_path = _optional_str(data.get("hashcat_path"), cls.hashcat_path)
        cls.wordlist_path = _optional_str(data.get("wordlist_path"), cls.wordlist_path)

    @classmethod
    def save(cls) -> None:
        text = (
            f"captures_dir = {_fmt(cls.captures_dir)}\n"
            f"save_pcap = {_fmt(cls.save_pcap)}\n"
            f"theme = {_fmt(cls.theme)}\n"
            f"log_level = {_fmt(cls.log_level)}\n"
            f"scanner_sort = {_fmt(cls.scanner_sort)}\n"
            f"scanner_sort_reverse = {_fmt(cls.scanner_sort_reverse)}\n"
            f"scanner_sort_delay = {_fmt(cls.scanner_sort_delay)}\n"
            f"silenced_bssids = {_fmt(cls.silenced_bssids)}\n"
            f"hide_silenced = {_fmt(cls.hide_silenced)}\n"
        )
        # TOML has no null: an unset path is an absent key, not an empty string.
        for key in ("hashcat_path", "wordlist_path"):
            value = getattr(cls, key)
            if value:
                text += f"{key} = {_fmt(value)}\n"
        if cls.decloaked_ssids:
            text += "\n[decloak]\n"
            for bssid, ssid in sorted(cls.decloaked_ssids.items()):
                text += f"{_fmt(bssid)} = {_fmt(ssid)}\n"
        try:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            _PATH.write_text(text, encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"Failed to save config at {_PATH}: {e}") from e


def _optional_str(raw: object, current: str | None) -> str | None:
    """A non-empty string from the file, else whatever is already set."""
    if raw is None:
        return current
    return str(raw) or None


def _fmt(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    s = str(v)
    if "'" not in s and "\n" not in s:
        return "'" + s + "'"
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
