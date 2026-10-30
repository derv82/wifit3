"""One AP's keys, hashlines and SSIDs across every SSID it has had, and its password status."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Optional

from wifit3.crack.hc22000_format import Hc22000Line, hashline_id, read_hc22000
from wifit3.crack.wpa_psk import psk_matches
from wifit3.models import AP_RECOVERED_CAPTURE_TYPES, PSK_CAPTURE_TYPES, CaptureType, PersistedCapture
from wifit3.persist.common import safe_ssid
from wifit3.persist.exhaustion import ExhaustedWordlist, read_exhausted_wordlists
from wifit3.persist.meta import (bssid_meta_paths, meta_path, read_bssid_metas, read_meta, update_meta,
                                 update_meta_file)
from wifit3.persist.pcap import read_pcap

_HASHLINES_KEY = "hashlines"
_LIVE_VERIFICATIONS_KEY = "live_verifications"
_MANUAL_CAPTURES_KEY = "manual_captures"
_PASSWORD_KEY_TYPES = frozenset({CaptureType.WPA_PSK, CaptureType.WEP})


class PasswordStatus(Enum):
    CURRENT = "CURRENT PASSWORD"
    NOT_VERIFIED = "NOT VERIFIED"
    STALE = "STALE PASSWORD"
    NOT_CRACKED = "NOT CRACKED"


@dataclass(frozen=True)
class DatedHashline:
    id: str
    raw: str
    line: Hc22000Line
    captured: tuple[int, ...]       # epoch seconds; empty when no record or pcap dates it
    dated_from_pcaps: bool = field(default=False, compare=False)    # no .meta record: dated from the pcaps

    @property
    def first_captured(self) -> Optional[int]:
        return min(self.captured, default=None)


@dataclass(frozen=True)
class LiveVerification:
    at: int
    correct: bool


@dataclass(frozen=True)
class Key:
    type: CaptureType               # the key's kind: WPA_PSK (also when recovered over WPS), WPS_PIN or WEP
    value: str                      # passphrase or raw PMK, WPS PIN, or WEP key hex
    captures: tuple[PersistedCapture, ...]  # every saved capture holding it, newest first
    hashline_ids: frozenset[str]
    live_verifications: tuple[LiveVerification, ...]
    manual_capture_names: frozenset[str] = frozenset()     # its captures typed in through Add key

    @property
    def id(self) -> tuple[CaptureType, str]:
        return self.type, self.value

    @property
    def newest_capture(self) -> PersistedCapture:
        return self.captures[0]

    @property
    def is_password(self) -> bool:
        return self.type in _PASSWORD_KEY_TYPES

    @property
    def ap_recovered_at(self) -> Optional[int]:
        """When the AP itself last gave the key up (WEP IVs, WPS), so it was correct then."""
        return next((c.timestamp for c in self.captures if c.type in AP_RECOVERED_CAPTURE_TYPES
                     and Path(c.path).name not in self.manual_capture_names), None)


@dataclass(frozen=True)
class SsidHistory:
    ssid: str
    first_seen: Optional[int]
    last_seen: Optional[int]


@dataclass(frozen=True)
class ApState:
    bssid: str
    ssids: tuple[SsidHistory, ...]          # newest first
    hashlines: tuple[DatedHashline, ...]    # newest first, undated last
    keys: tuple[Key, ...]                   # most recently confirmed first
    status: PasswordStatus
    status_key: Optional[Key]
    key_statuses: Mapping[tuple[CaptureType, str], PasswordStatus]    # by Key.id; see ``key_statuses``
    hashline_capture: Optional[PersistedCapture]    # the newest SSID's .hc22000; jobs are filed under it
    uncracked: tuple[DatedHashline, ...]            # the hashlines no key matches, newest first
    exhausted_wordlists: Mapping[str, frozenset[ExhaustedWordlist]]   # by uncracked hashline id
    handshake_and_pmkid_files: tuple[PersistedCapture, ...]    # one per file, crackable or not, newest first


def epoch_sort_key(epoch: Optional[int]) -> tuple[bool, int]:
    """Sort key for an optional epoch; with ``reverse=True``, newest first and undated last."""
    return epoch is not None, epoch or 0


def _pcap_dates(lines: dict[str, Hc22000Line], pcaps: Iterable[Path]) -> dict[str, set[int]]:
    """Per hashline id, the time of each pcap frame carrying its MIC or PMKID."""
    needles = {hid: bytes.fromhex(line.pmkid_or_mic) for hid, line in lines.items()}
    found: dict[str, set[int]] = {}
    for pcap in pcaps:
        for frame, epoch in read_pcap(pcap):
            for hid, needle in needles.items():
                if needle in frame:
                    found.setdefault(hid, set()).add(epoch)
    return found


def _stored_dates(metas: list[dict]) -> dict[str, set[int]]:
    """Dates per hashline id with a .meta record; an empty set records that none were found."""
    found: dict[str, set[int]] = {}
    for meta in metas:
        stored = meta.get(_HASHLINES_KEY)
        if not isinstance(stored, dict):
            continue
        for hid, record in stored.items():
            captured = record.get("captured") if isinstance(record, dict) else None
            if isinstance(captured, list):
                found.setdefault(hid, set()).update(e for e in captured if isinstance(e, int))
    return found


def _capture_type(value: object) -> Optional[CaptureType]:
    try:
        return CaptureType(value)
    except ValueError:
        return None


def _stored_verifications(metas: list[dict]) -> dict[tuple[CaptureType, str], list[LiveVerification]]:
    found: dict[tuple[CaptureType, str], list[LiveVerification]] = {}
    for meta in metas:
        stored = meta.get(_LIVE_VERIFICATIONS_KEY)
        if not isinstance(stored, list):
            continue
        for record in stored:
            if not isinstance(record, dict):
                continue
            kind, value, at = _capture_type(record.get("kind")), record.get("key"), record.get("at")
            if kind is not None and isinstance(value, str) and isinstance(at, int):
                found.setdefault((kind, value), []).append(LiveVerification(at, bool(record.get("correct"))))
    return found


def _manual_capture_names(metas: list[dict]) -> frozenset[str]:
    names: set[str] = set()
    for meta in metas:
        stored = meta.get(_MANUAL_CAPTURES_KEY)
        if isinstance(stored, list):
            names.update(name for name in stored if isinstance(name, str))
    return frozenset(names)


def read_hashlines(bssid: str, captures: list[PersistedCapture]) -> dict[str, tuple[str, Hc22000Line]]:
    """The AP's lines in its captures' .hc22000 files, ``{hashline id: (raw, parsed)}``."""
    compact_bssid = bssid.replace(":", "").lower()
    found: dict[str, tuple[str, Hc22000Line]] = {}
    for path in dict.fromkeys(c.path for c in captures if c.path.endswith(".hc22000")):
        for raw, line in read_hc22000(path):
            if line.mac_ap == compact_bssid:
                found.setdefault(hashline_id(raw), (raw, line))
    return found


def _keys_in_capture(capture: PersistedCapture) -> list[tuple[CaptureType, str]]:
    keys = []
    if capture.type == CaptureType.WEP and capture.value:
        keys.append((CaptureType.WEP, capture.value))
    if capture.type == CaptureType.WPS_PIN and capture.pin is not None:
        keys.append((CaptureType.WPS_PIN, capture.pin))
    if capture.type in PSK_CAPTURE_TYPES and capture.value:
        keys.append((CaptureType.WPA_PSK, capture.value))
    return keys


def _key_captures(captures: list[PersistedCapture]) -> dict[tuple[CaptureType, str], list[PersistedCapture]]:
    """Every saved key, ``(kind, value)``, and the captures holding it, newest first."""
    found: dict[tuple[CaptureType, str], list[PersistedCapture]] = {}
    for capture in sorted(captures, key=lambda c: c.timestamp, reverse=True):
        for kind_and_value in _keys_in_capture(capture):
            found.setdefault(kind_and_value, []).append(capture)
    return found


def _confirmations(key: Key, hashlines: Iterable[DatedHashline]) -> list[Optional[int]]:
    """Dates of the authorized hashlines (None when undated), correct live checks and AP recoveries
    that confirm the key. An M1+M2 match doesn't: it may be a client's wrong password."""
    dates = [h.first_captured for h in hashlines if h.id in key.hashline_ids and h.line.authorized]
    dates += [v.at for v in key.live_verifications if v.correct]
    if key.ap_recovered_at is not None:
        dates.append(key.ap_recovered_at)
    return dates


def confirmed_at(key: Key, hashlines: Iterable[DatedHashline]) -> Optional[int]:
    """When the key was last confirmed; None if nothing dated has (see ``is_confirmed``)."""
    return max((d for d in _confirmations(key, hashlines) if d is not None), default=None)


def is_confirmed(key: Key, hashlines: Iterable[DatedHashline]) -> bool:
    return bool(_confirmations(key, hashlines))


def _matching_key(hashline: DatedHashline, keys: list[Key]) -> Optional[Key]:
    return next((k for k in keys if hashline.id in k.hashline_ids), None)


def password_status(hashlines: Iterable[DatedHashline],
                    keys: Iterable[Key]) -> tuple[PasswordStatus, Optional[Key]]:
    """Judged on the newest authorized hashline, correct live check, or AP recovery of a PSK or WEP
    key, else the newest M1+M2 hashline. ``hashlines`` newest first, ``keys`` newest confirmed first."""
    hashlines = list(hashlines)
    keys = [k for k in keys if k.is_password]
    if not keys:
        return PasswordStatus.NOT_CRACKED, None

    proofs: list[tuple[Optional[int], Optional[Key]]] = []
    for h in hashlines:
        if h.line.authorized:
            proofs.append((h.first_captured, _matching_key(h, keys)))
    for key in keys:
        for v in key.live_verifications:
            if v.correct:
                proofs.append((v.at, key))
        if key.ap_recovered_at is not None:
            proofs.append((key.ap_recovered_at, key))

    if not proofs:
        for h in hashlines:
            key = _matching_key(h, keys)
            if key is not None:
                return PasswordStatus.NOT_VERIFIED, key
        return PasswordStatus.NOT_VERIFIED, keys[0]

    proofs.sort(key=lambda p: epoch_sort_key(p[0]), reverse=True)
    newest_at, newest_key = proofs[0]
    if newest_key is None:
        for _, key in proofs:
            if key is not None:
                return PasswordStatus.STALE, key
        return PasswordStatus.STALE, None

    for v in newest_key.live_verifications:
        if not v.correct and epoch_sort_key(v.at) > epoch_sort_key(newest_at):
            return PasswordStatus.STALE, newest_key
    return PasswordStatus.CURRENT, newest_key


def key_statuses(keys: Iterable[Key], hashlines: Iterable[DatedHashline], status: PasswordStatus,
                 status_key: Optional[Key]) -> Mapping[tuple[CaptureType, str], PasswordStatus]:
    """By Key.id: the AP's status for its status key; else NOT_VERIFIED when unconfirmed, CURRENT
    for a confirmed PIN, STALE for a confirmed password key (newer evidence won)."""
    hashlines = list(hashlines)
    statuses: dict[tuple[CaptureType, str], PasswordStatus] = {}
    for key in keys:
        if key == status_key:
            statuses[key.id] = status
        elif not is_confirmed(key, hashlines):
            statuses[key.id] = PasswordStatus.NOT_VERIFIED
        else:
            statuses[key.id] = PasswordStatus.STALE if key.is_password else PasswordStatus.CURRENT
    return MappingProxyType(statuses)


def load_ap_state(bssid: str, captures: list[PersistedCapture]) -> ApState:
    """``captures``: the AP's indexed captures (every SSID). Hashlines with no .meta record are
    dated from the AP's pcaps."""
    metas = read_bssid_metas(bssid)
    lines = read_hashlines(bssid, captures)
    dates = _stored_dates(metas)
    unrecorded = {hid: line for hid, (_, line) in lines.items() if hid not in dates}
    if unrecorded:
        pcaps = [Path(c.path) for c in captures if c.path.endswith(".pcap")]
        dates.update({hid: set() for hid in unrecorded} | _pcap_dates(unrecorded, pcaps))

    hashlines = sorted(
        (DatedHashline(hid, raw, line, tuple(sorted(dates[hid])), dated_from_pcaps=hid in unrecorded)
         for hid, (raw, line) in lines.items()),
        key=lambda h: epoch_sort_key(h.first_captured), reverse=True)
    verifications = _stored_verifications(metas)
    manual_capture_names = _manual_capture_names(metas)
    keys = []
    for (key_type, value), key_captures in _key_captures(captures).items():
        matched = frozenset(h.id for h in hashlines
                            if key_type == CaptureType.WPA_PSK and psk_matches(value, h.line))
        keys.append(Key(key_type, value, tuple(key_captures), matched,
                        tuple(sorted(verifications.get((key_type, value), []), key=lambda v: v.at)),
                        manual_capture_names.intersection(Path(c.path).name for c in key_captures)))
    keys.sort(key=lambda k: epoch_sort_key(confirmed_at(k, hashlines)), reverse=True)

    status, status_key = password_status(hashlines, keys)
    ssids = _ssid_histories(hashlines, keys)
    newest_ssid = safe_ssid(ssids[0].ssid) if ssids else None
    hc22000 = sorted((c for c in captures if c.path.endswith(".hc22000")),
                     key=lambda c: (safe_ssid(c.ssid) == newest_ssid, c.timestamp), reverse=True)
    cracked = {hid for key in keys for hid in key.hashline_ids}
    uncracked = tuple(h for h in hashlines if h.id not in cracked)
    exhausted = read_exhausted_wordlists(metas, [h.id for h in uncracked]) if uncracked else {}
    handshake_and_pmkid_files = {c.path: c for c in sorted(captures, key=lambda c: c.timestamp)
                                 if c.type in (CaptureType.HS, CaptureType.PMKID)}
    return ApState(bssid, ssids, tuple(hashlines), tuple(keys), status, status_key,
                   key_statuses(keys, hashlines, status, status_key),
                   hc22000[0] if hc22000 else None, uncracked, MappingProxyType(exhausted),
                   tuple(reversed(handshake_and_pmkid_files.values())))


def _ssid_histories(hashlines: list[DatedHashline], keys: list[Key]) -> tuple[SsidHistory, ...]:
    seen: dict[str, list[int]] = {}
    for h in hashlines:
        seen.setdefault(h.line.ssid, []).extend(h.captured)
    for key in keys:
        for capture in key.captures:
            if capture.ssid and capture.ssid not in seen:
                seen[capture.ssid] = [capture.timestamp]
    histories = [SsidHistory(ssid, min(epochs, default=None), max(epochs, default=None))
                 for ssid, epochs in seen.items()]
    return tuple(sorted(histories, key=lambda s: epoch_sort_key(s.last_seen), reverse=True))


def record_hashlines_captured(ssid: Optional[str], bssid: str, raw_lines: Iterable[str]) -> None:
    """Note in the SSID's .meta that these hashlines were captured, now."""
    now = int(time.time())
    record_hashline_dates(ssid, bssid, {hashline_id(raw): [now] for raw in raw_lines})


def record_hashline_dates(ssid: Optional[str], bssid: str, dates: Mapping[str, Iterable[int]]) -> None:
    """Add capture dates, by hashline id, to the SSID's .meta; no dates records that none exist."""
    stored = read_meta(meta_path(ssid, bssid)).get(_HASHLINES_KEY)
    stored = stored if isinstance(stored, dict) else {}
    for hid, epochs in dates.items():
        if not isinstance(stored.get(hid), dict):
            stored[hid] = {}
        if not isinstance(stored[hid].get("captured"), list):
            stored[hid]["captured"] = []
        captured = stored[hid]["captured"]
        captured.extend(e for e in epochs if e not in captured)
    update_meta(ssid, bssid, **{_HASHLINES_KEY: stored})


def record_manual_capture(ssid: Optional[str], bssid: str, path: Path) -> None:
    """Note in the SSID's .meta that this capture holds a key typed in through Add key."""
    stored = read_meta(meta_path(ssid, bssid)).get(_MANUAL_CAPTURES_KEY)
    stored = [name for name in stored if isinstance(name, str)] if isinstance(stored, list) else []
    if path.name not in stored:
        stored.append(path.name)
    update_meta(ssid, bssid, **{_MANUAL_CAPTURES_KEY: stored})


def record_live_verification(key: Key, correct: bool) -> None:
    """Note in the .meta of the key's newest capture that a live check accepted or rejected it."""
    capture = key.newest_capture
    stored = read_meta(meta_path(capture.ssid, capture.bssid)).get(_LIVE_VERIFICATIONS_KEY)
    stored = stored if isinstance(stored, list) else []
    stored.append({"kind": key.type.value, "key": key.value, "at": int(time.time()), "correct": correct})
    update_meta(capture.ssid, capture.bssid, **{_LIVE_VERIFICATIONS_KEY: stored})


def forget_key(key: Key) -> None:
    """Drop the key's live checks, and the Add key marks of its captures, from every SSID's .meta."""
    capture_names = {Path(c.path).name for c in key.captures}
    for path in bssid_meta_paths(key.newest_capture.bssid):
        meta = read_meta(path)
        fields: dict[str, Optional[list]] = {}
        verifications = meta.get(_LIVE_VERIFICATIONS_KEY)
        if isinstance(verifications, list):
            kept = [r for r in verifications if not (isinstance(r, dict) and r.get("key") == key.value
                                                     and _capture_type(r.get("kind")) == key.type)]
            if len(kept) != len(verifications):
                fields[_LIVE_VERIFICATIONS_KEY] = kept or None
        manual = meta.get(_MANUAL_CAPTURES_KEY)
        if isinstance(manual, list):
            kept_names = [name for name in manual if name not in capture_names]
            if len(kept_names) != len(manual):
                fields[_MANUAL_CAPTURES_KEY] = kept_names or None
        if fields:
            update_meta_file(path, **fields)
