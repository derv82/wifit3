"""Which wordlists ran to the end against which hashlines, kept in the AP's .meta file."""
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from wifit3.crack.hc22000_format import parse_hc22000
from wifit3.crack.passwords import SSID_VARIANTS_VERSION
from wifit3.models import PersistedCapture
from wifit3.persist.meta import meta_path, read_meta, update_meta

SSID_VARIANTS_IDENTITY = f"ssid-variants:v{SSID_VARIANTS_VERSION}"
_EXHAUSTED_KEY = "exhausted"


@dataclass(frozen=True)
class Exhaustion:
    exhausted_hashlines: int
    hashlines: int
    latest: int             # epoch seconds of the newest record among the exhausted hashlines


def hashline_id(line: str) -> str:
    return hashlib.sha256(line.strip().lower().encode()).hexdigest()[:16]


def capture_hashline_ids(capture_path: str) -> list[str]:
    """``hashline_id`` of each hashline in the capture file, deduped."""
    text = Path(capture_path).read_text(encoding="utf-8", errors="replace")
    return list(dict.fromkeys(hashline_id(line) for line in text.splitlines()
                              if parse_hc22000(line) is not None))


def wordlist_identity(path: str | Path) -> str:
    """Size and mtime, so a moved or renamed wordlist keeps its identity."""
    stat = Path(path).stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _exhausted(meta: dict) -> dict[str, dict[str, int]]:
    stored = meta.get(_EXHAUSTED_KEY)
    if not isinstance(stored, dict):
        return {}
    return {identity: {hashline: epoch for hashline, epoch in recorded.items()
                       if isinstance(epoch, int)}
            for identity, recorded in stored.items() if isinstance(recorded, dict)}


def record_exhausted(ssid: Optional[str], bssid: str, identity: str,
                     hashline_ids: list[str]) -> None:
    """Note in the AP's .meta that ``identity`` ran out on these hashlines, now."""
    if not hashline_ids:
        return
    meta = read_meta(meta_path(ssid, bssid))
    exhausted = _exhausted(meta)
    recorded = exhausted.setdefault(identity, {})
    now = int(time.time())
    for hashline in hashline_ids:
        recorded[hashline] = max(recorded.get(hashline, 0), now)
    saved_ssid = meta.get("ssid")
    update_meta(saved_ssid if isinstance(saved_ssid, str) else ssid, bssid,
                **{_EXHAUSTED_KEY: exhausted})


def capture_exhaustion(capture: PersistedCapture) -> dict[str, Exhaustion]:
    """Per identity that ran out on any of the capture's current hashlines."""
    hashline_ids = capture_hashline_ids(capture.path)
    exhausted = _exhausted(read_meta(meta_path(capture.ssid, capture.bssid)))
    found: dict[str, Exhaustion] = {}
    for identity, recorded in exhausted.items():
        epochs = [recorded[hashline] for hashline in hashline_ids if hashline in recorded]
        if epochs:
            found[identity] = Exhaustion(len(epochs), len(hashline_ids), max(epochs))
    return found
