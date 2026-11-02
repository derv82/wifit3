"""Which wordlists ran to the end against which hashlines, kept in the AP's .meta file."""
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from wifit3.crack.hc22000_format import hashline_id, read_hc22000
from wifit3.crack.passwords import DEFAULT_PASSWORDS_PATH, SSID_VARIANTS_VERSION, expand_wordlists
from wifit3.persist.config import Config
from wifit3.persist.meta import meta_path, read_meta, update_meta

SSID_VARIANTS_IDENTITY = f"ssid-variants:v{SSID_VARIANTS_VERSION}"
SSID_VARIANTS_NAME = "SSID variants"
_EXHAUSTED_KEY = "exhausted"
_WORDLISTS_KEY = "wordlists"


@dataclass(frozen=True)
class Exhaustion:
    identity: str
    wordlist_names: tuple[str, ...]     # as recorded, else the configured wordlist's; empty if unknown
    exhausted_hashlines: int
    hashlines: int
    latest: int             # epoch seconds of the newest record among the exhausted hashlines


@dataclass(frozen=True)
class ExhaustedWordlist:
    identity: str
    names: tuple[str, ...]      # as recorded, else the configured wordlist's; empty if unknown


def capture_hashline_ids(capture_path: str) -> list[str]:
    """``hashline_id`` of each hashline in the capture file, deduped."""
    return list(dict.fromkeys(hashline_id(raw) for raw, _ in read_hc22000(capture_path)))


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


def _wordlist_names(meta: dict) -> dict[str, list[str]]:
    stored = meta.get(_WORDLISTS_KEY)
    if not isinstance(stored, dict):
        return {}
    return {identity: [n for n in names if isinstance(n, str)]
            for identity, names in stored.items() if isinstance(names, list)}


def record_exhausted(ssid: Optional[str], bssid: str, identity: str, wordlist_name: str,
                     hashline_ids: list[str]) -> None:
    """Note in the AP's .meta that ``identity`` (named ``wordlist_name``) ran out on these
    hashlines, now."""
    if not hashline_ids:
        return
    meta = read_meta(meta_path(ssid, bssid))
    exhausted = _exhausted(meta)
    recorded = exhausted.setdefault(identity, {})
    now = int(time.time())
    for hashline in hashline_ids:
        recorded[hashline] = max(recorded.get(hashline, 0), now)
    names = _wordlist_names(meta)
    if wordlist_name and wordlist_name not in names.setdefault(identity, []):
        names[identity].append(wordlist_name)
    saved_ssid = meta.get("ssid")
    update_meta(saved_ssid if isinstance(saved_ssid, str) else ssid, bssid,
                **{_EXHAUSTED_KEY: exhausted, _WORDLISTS_KEY: names})


def _configured_wordlist_names() -> dict[str, str]:
    """Names for records from before names were stored: the configured wordlists on disk now."""
    names = {SSID_VARIANTS_IDENTITY: SSID_VARIANTS_NAME}
    for wordlist in expand_wordlists([str(DEFAULT_PASSWORDS_PATH), *Config.hashcat.wordlists,
                                      *Config.cpu_crack.wordlists]):
        try:
            names.setdefault(wordlist_identity(wordlist), Path(wordlist).name)
        except OSError:
            continue
    return names


def _merged_records(metas: list[dict]) -> tuple[dict[str, dict[str, int]], dict[str, list[str]]]:
    """Every SSID's exhaustion records and wordlist names, merged."""
    exhausted: dict[str, dict[str, int]] = {}
    names: dict[str, list[str]] = {}
    for meta in metas:
        for identity, recorded in _exhausted(meta).items():
            merged = exhausted.setdefault(identity, {})
            for hashline, epoch in recorded.items():
                merged[hashline] = max(merged.get(hashline, 0), epoch)
        for identity, stored in _wordlist_names(meta).items():
            names.setdefault(identity, []).extend(n for n in stored if n not in names[identity])
    return exhausted, names


def _names_for(identities: Iterable[str], names: dict[str, list[str]]) -> dict[str, tuple[str, ...]]:
    """Recorded names, else the configured wordlist's (stat'ed only when one is unnamed)."""
    unnamed = [identity for identity in identities if not names.get(identity)]
    configured = _configured_wordlist_names() if unnamed else {}
    resolved = {identity: tuple(names.get(identity, ())) for identity in identities}
    resolved.update({identity: (configured[identity],) for identity in unnamed if identity in configured})
    return resolved


def read_exhaustion(metas: list[dict], hashline_ids: Iterable[str]) -> dict[str, Exhaustion]:
    """Per wordlist identity that ran out on any of these hashlines, from the AP's .meta files
    (every SSID's, see ``read_bssid_metas``)."""
    exhausted, names = _merged_records(metas)
    wanted = list(dict.fromkeys(hashline_ids))
    epochs_by_identity = {identity: epochs for identity, recorded in exhausted.items()
                          if (epochs := [recorded[hashline] for hashline in wanted if hashline in recorded])}
    resolved = _names_for(epochs_by_identity, names)
    return {identity: Exhaustion(identity, resolved[identity], len(epochs), len(wanted), max(epochs))
            for identity, epochs in epochs_by_identity.items()}


def read_exhausted_wordlists(metas: list[dict],
                             hashline_ids: Iterable[str]) -> dict[str, frozenset[ExhaustedWordlist]]:
    """Per hashline id, the wordlists that ran out on it; hashlines with none are left out."""
    exhausted, names = _merged_records(metas)
    wanted = set(hashline_ids)
    identities_by_hashline: dict[str, set[str]] = {}
    for identity, recorded in exhausted.items():
        for hashline in wanted.intersection(recorded):
            identities_by_hashline.setdefault(hashline, set()).add(identity)
    resolved = _names_for({i for ids in identities_by_hashline.values() for i in ids}, names)
    return {hashline: frozenset(ExhaustedWordlist(identity, resolved[identity]) for identity in identities)
            for hashline, identities in identities_by_hashline.items()}
