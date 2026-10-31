"""A potfile in DWPA's format: one ``BSSID:MAC_STA:SSID:PASS`` line per key."""
import re
from dataclasses import dataclass
from pathlib import Path

from wifit3.crack.passwords import is_valid_psk
from wifit3.persist.common import bssid_to_dashed, safe_ssid

_BSSID = re.compile(r"[0-9a-f]{12}")
POTFILE_RE = re.compile(
    r"^(?P<ssid>.+)_(?P<bssid>[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})\.potfile$")


def potfile_path(captures_dir: Path, ssid: str | None, bssid: str) -> Path:
    """The AP's potfile: its existing one whatever SSID it was saved under, else a new path."""
    existing = sorted(captures_dir.glob(f"*_{bssid_to_dashed(bssid)}.potfile"))
    return existing[0] if existing else captures_dir / f"{safe_ssid(ssid)}_{bssid_to_dashed(bssid)}.potfile"


@dataclass(frozen=True)
class PotfileEntry:
    bssid: str
    mac_sta: str
    ssid: str
    key: str


def parse_potfile(text: str) -> list[PotfileEntry]:
    """The usable entries of a potfile, without duplicates."""
    entries = (entry for entry in map(_entry, text.splitlines()) if entry is not None)
    return list(dict.fromkeys(entries))


def potfile_line(entry: PotfileEntry) -> str:
    bssid = entry.bssid.replace(":", "").lower()
    mac_sta = entry.mac_sta.replace(":", "").lower()
    return f"{bssid}:{mac_sta}:{entry.ssid}:{entry.key}"


def read_potfile(path: Path) -> list[PotfileEntry]:
    try:
        return parse_potfile(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return []


def write_potfile(path: Path, entries: list[PotfileEntry]) -> None:
    """Replace the file's contents with ``entries``; the file goes away with its last entry."""
    if not entries:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{potfile_line(entry)}\n" for entry in entries), encoding="utf-8")


def remove_potfile_key(path: Path, key: str) -> None:
    """Drop every line carrying ``key``, keeping the AP's other keys."""
    write_potfile(path, [entry for entry in read_potfile(path) if entry.key != key])


def missing_entries(existing: list[PotfileEntry],
                    entries: list[PotfileEntry]) -> list[PotfileEntry]:
    """``entries`` whose key ``existing`` does not have yet, deduplicated by key."""
    known = {entry.key for entry in existing}
    missing = []
    for entry in entries:
        if entry.key in known:
            continue
        known.add(entry.key)
        missing.append(entry)
    return missing


def _entry(line: str) -> PotfileEntry | None:
    fields = line.split(":", 3)
    if len(fields) != 4:
        return None
    bssid = _colon_bssid(fields[0])
    ssid, key = fields[2], fields[3]
    if bssid is None or not ssid or not is_valid_psk(key):
        return None
    return PotfileEntry(bssid, _colon_bssid(fields[1]) or "", ssid, key)


def _colon_bssid(text: str) -> str | None:
    hex_bssid = text.strip().lower()
    if _BSSID.fullmatch(hex_bssid) is None:
        return None
    return ":".join(hex_bssid[index:index + 2] for index in range(0, 12, 2))
