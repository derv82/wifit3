"""Per SSID+BSSID ``.meta`` JSON file in captures/, named like the AP's captures."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from wifit3.persist.common import bssid_to_colon, bssid_to_dashed, safe_ssid
from wifit3.persist.config import Config


def meta_path(ssid: Optional[str], bssid: str) -> Path:
    """captures_dir/<safe_ssid>_<bssid-dashed>.meta"""
    return Path(Config.captures_dir) / f"{safe_ssid(ssid)}_{bssid_to_dashed(bssid)}.meta"


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
    return path
