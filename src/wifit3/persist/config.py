"""Persistent user preferences: a TOML file in the OS config dir, one table per cracking tool."""
from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from types import UnionType
from typing import Any, TypeVar, get_args, get_origin, get_type_hints

from platformdirs import user_config_dir

from wifit3.dot11 import SSID_MAX_OCTETS
from wifit3.models.jobs import ToolName

_PATH = Path(user_config_dir("wifit3", appauthor=False)) / "config.toml"


class ConfigError(Exception):
    pass


@dataclass
class CpuCrackSettings:
    wordlists: list[str] = field(default_factory=list)
    default_passwords: bool = True
    ap_ssid_passwords: bool = True
    probed_ssid_passwords: bool = True


@dataclass
class HashcatSettings:
    path: str | None = None
    args: str | None = None
    wordlists: list[str] = field(default_factory=list)


@dataclass
class HashtopolisSettings:
    url: str | None = None
    access_key: str | None = None
    wordlist_id: int | None = None
    wordlist_name: str | None = None
    access_group_id: int | None = None
    access_group_name: str | None = None


SettingsT = TypeVar("SettingsT", CpuCrackSettings, HashcatSettings, HashtopolisSettings)


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
    wps_auto_pbc: bool = True
    auto_crack: ToolName | None = ToolName.CPU_CRACK
    cpu_crack: CpuCrackSettings = CpuCrackSettings()
    hashcat: HashcatSettings = HashcatSettings()
    hashtopolis: HashtopolisSettings = HashtopolisSettings()

    @classmethod
    def is_silenced(cls, bssid: str) -> bool:
        return bssid.lower() in cls.silenced_bssids

    @classmethod
    def decloaked_ssid(cls, bssid: str) -> str | None:
        return cls.decloaked_ssids.get(bssid.lower())

    @classmethod
    def remember_decloak(cls, bssid: str, ssid: str) -> bool:
        """Store a confirmed BSSID-to-SSID reveal; whether it is new. A stored SSID is only
        ever offered as a candidate: a BSSID can be reassigned, and SSIDs get renamed."""
        if not ssid or len(ssid.encode("utf-8")) > SSID_MAX_OCTETS:
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
        stored = data.get("decloak")
        if isinstance(stored, dict):
            cls.decloaked_ssids = {str(bssid).lower(): ssid for bssid, ssid in stored.items()
                                   if isinstance(ssid, str) and ssid
                                   and len(ssid.encode("utf-8")) <= SSID_MAX_OCTETS}
        cls.hide_silenced = bool(data.get("hide_silenced", cls.hide_silenced))
        cls.wps_auto_pbc = bool(data.get("wps_auto_pbc", cls.wps_auto_pbc))
        cls.auto_crack = _auto_crack(data, cls.auto_crack)
        legacy_wordlists = _legacy_wordlists(data)
        cls.cpu_crack = _load_table(_table(data, "cpu_crack"), cls.cpu_crack)
        cls.hashcat = _load_table(
            {"path": data.get("hashcat_path"), "args": data.get("hashcat_args"),
             "wordlists": legacy_wordlists, **_table(data, "hashcat")}, cls.hashcat)
        legacy_hashtopolis = {name.removeprefix("hashtopolis_"): value
                              for name, value in data.items() if name.startswith("hashtopolis_")}
        cls.hashtopolis = _load_table({**legacy_hashtopolis, **_table(data, "hashtopolis")},
                                      cls.hashtopolis)

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
            f"wps_auto_pbc = {_fmt(cls.wps_auto_pbc)}\n"
            f"auto_crack = {_fmt(cls.auto_crack or '')}\n"
        )
        text += _table_text("cpu_crack", cls.cpu_crack)
        text += _table_text("hashcat", cls.hashcat)
        text += _table_text("hashtopolis", cls.hashtopolis)
        if cls.decloaked_ssids:
            text += "\n[decloak]\n"
            for bssid, ssid in sorted(cls.decloaked_ssids.items()):
                text += f"{_fmt(bssid)} = {_fmt(ssid)}\n"
        try:
            _PATH.parent.mkdir(parents=True, exist_ok=True)
            _PATH.write_text(text, encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"Failed to save config at {_PATH}: {e}") from e


def _auto_crack(data: dict, current: ToolName | None) -> ToolName | None:
    """An empty or unknown tool name is None; a legacy ``auto_crack = true`` is CPU Crack."""
    raw = data["auto_crack_tool"] if "auto_crack_tool" in data else data.get("auto_crack", current)
    if raw is None or isinstance(raw, bool):
        return ToolName.CPU_CRACK if raw else None
    try:
        return ToolName(raw)
    except ValueError:
        return None


def _legacy_wordlists(data: dict) -> object:
    if "wordlist_paths" in data:
        return data["wordlist_paths"]
    return [data["wordlist_path"]] if data.get("wordlist_path") else None


def _table(data: dict, name: str) -> dict:
    table = data.get(name)
    return table if isinstance(table, dict) else {}


def _load_table(raw: dict, current: SettingsT) -> SettingsT:
    """``current`` with each value from ``raw`` of its field's type; the rest are dropped."""
    hints = get_type_hints(type(current))
    loaded = {f.name: raw[f.name] for f in fields(current)
              if raw.get(f.name) not in (None, "") and _has_type(raw[f.name], hints[f.name])}
    return replace(current, **loaded)


def _has_type(value: Any, hint: Any) -> bool:
    if isinstance(hint, UnionType):
        return any(_has_type(value, arg) for arg in get_args(hint) if arg is not type(None))
    if get_origin(hint) is list:
        return isinstance(value, list) and all(_has_type(v, get_args(hint)[0]) for v in value)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, hint)


def _table_text(name: str, settings: SettingsT) -> str:
    """The ``[name]`` table; TOML has no null, so an unset value is an absent key."""
    lines = [f"{key} = {_fmt(value)}\n" for key, value in asdict(settings).items()
             if value not in (None, "", [])]
    return f"\n[{name}]\n" + "".join(lines) if lines else ""


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
