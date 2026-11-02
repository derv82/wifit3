"""Per SSID+BSSID ``.meta`` JSON file in captures/, named like the AP's captures."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from wifit3.persist.common import bssid_to_colon, bssid_to_dashed, safe_ssid
from wifit3.persist.config import Config


def meta_path(ssid: Optional[str], bssid: str) -> Path:
    """captures_dir/<safe_ssid>_<bssid-dashed>.meta"""
    return Path(Config.captures_dir) / f"{safe_ssid(ssid)}_{bssid_to_dashed(bssid)}.meta"


_META_SUFFIX_LEN = len("_aa-bb-cc-dd-ee-ff.meta")


class _MetaPaths:
    """The .meta files in captures/ by dashed BSSID; scanned once per captures_dir."""

    def __init__(self) -> None:
        self._by_bssid: dict[str, set[Path]] = {}
        self._scanned_dir: Optional[str] = None

    def for_bssid(self, dashed: str) -> set[Path]:
        return self._scanned().get(dashed, set())

    def add(self, path: Path) -> None:
        if (dashed := _meta_bssid(path.name)) is not None and path.parent == Path(Config.captures_dir):
            self._scanned().setdefault(dashed, set()).add(path)

    def clear(self) -> None:
        self._scanned_dir = None

    def _scanned(self) -> dict[str, set[Path]]:
        captures_dir = Config.captures_dir
        if self._scanned_dir != captures_dir:
            found: dict[str, set[Path]] = {}
            try:
                with os.scandir(captures_dir) as entries:
                    for entry in entries:
                        if (dashed := _meta_bssid(entry.name)) is not None:
                            found.setdefault(dashed, set()).add(Path(captures_dir) / entry.name)
            except FileNotFoundError:
                pass
            self._by_bssid, self._scanned_dir = found, captures_dir
        return self._by_bssid


def _meta_bssid(name: str) -> Optional[str]:
    if len(name) < _META_SUFFIX_LEN or not name.endswith(".meta") or name[-_META_SUFFIX_LEN] != "_":
        return None
    return name[-_META_SUFFIX_LEN + 1:-len(".meta")]


_meta_paths = _MetaPaths()


def bssid_meta_paths(bssid: str) -> list[Path]:
    """Every SSID's .meta for this BSSID."""
    return sorted(path for path in _meta_paths.for_bssid(bssid_to_dashed(bssid)).copy() if path.exists())


def clear_meta_paths_cache() -> None:
    """Scan captures/ again for .meta files on the next lookup; ``_write_meta`` adds the ones it creates."""
    _meta_paths.clear()


def read_bssid_metas(bssid: str) -> list[dict[str, Any]]:
    """Every SSID's .meta for this BSSID, read (see ``read_meta``)."""
    return [read_meta(path) for path in bssid_meta_paths(bssid)]


def read_meta(path: Path) -> dict[str, Any]:
    """The file's JSON object, or an empty dict when missing or invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def update_meta(ssid: Optional[str], bssid: str, **fields: Any) -> Path:
    """Merge ``fields`` into the AP's .meta (a None value removes that key)."""
    path = meta_path(ssid, bssid)
    data = read_meta(path)
    data["bssid"] = bssid_to_colon(bssid)
    if ssid and ssid != "<hidden>":
        data["ssid"] = ssid
    _write_meta(path, data, fields)
    return path


def update_meta_file(path: Path, **fields: Any) -> None:
    """Merge ``fields`` into the .meta at ``path`` (a None value removes that key)."""
    _write_meta(path, read_meta(path), fields)


def _write_meta(path: Path, data: dict[str, Any], fields: dict[str, Any]) -> None:
    for key, value in fields.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    temporary.replace(path)
    _meta_paths.add(path)
