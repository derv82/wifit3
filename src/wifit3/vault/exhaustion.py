import hashlib
import json
from pathlib import Path

from wifit3.crack.hc22000_format import Hc22000Line, parse_hc22000
from wifit3.persist.metadata import load_metadata, metadata_path


_HISTORY_KEY = "exhausted_wordlists"


def wordlist_identity(path: str | Path) -> str:
    stat = Path(path).stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _canonical_record(record: Hc22000Line) -> bytes:
    fields = (
        "WPA", record.kind.lower(), record.pmkid_or_mic.lower(), record.mac_ap.lower(),
        record.mac_sta.lower(), record.essid.lower(), record.anonce.lower(), record.eapol.lower(),
        record.message_pair.lower(),
    )
    return "*".join(fields).encode("utf-8")


def record_id(line: str) -> str | None:
    record = parse_hc22000(line)
    if record is None:
        return None
    return hashlib.sha256(_canonical_record(record)).hexdigest()


def capture_record_ids(path: str | Path) -> set[str]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return {identifier for line in text.splitlines() if (identifier := record_id(line)) is not None}


def exhausted_wordlists(bssid: str, record_ids: set[str]) -> set[str]:
    if not record_ids:
        return set()
    history = load_metadata(metadata_path(bssid)).get(_HISTORY_KEY)
    if not isinstance(history, dict):
        return set()
    checksums: set[str] | None = None
    for identifier in record_ids:
        recorded = history.get(identifier)
        if not isinstance(recorded, list):
            return set()
        values = {value for value in recorded if isinstance(value, str)}
        checksums = values if checksums is None else checksums & values
    return checksums or set()


def record_exhaustion(bssid: str, record_ids: set[str], checksum: str) -> None:
    if not record_ids:
        return
    path = metadata_path(bssid)
    metadata = load_metadata(path)
    history = metadata.get(_HISTORY_KEY)
    if not isinstance(history, dict):
        history = {}
    for identifier in record_ids:
        recorded = history.get(identifier)
        values = {value for value in recorded if isinstance(value, str)} if isinstance(recorded, list) else set()
        values.add(checksum)
        history[identifier] = sorted(values)
    metadata[_HISTORY_KEY] = history
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".meta.tmp")
    temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    temporary.replace(path)
