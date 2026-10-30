"""In-memory index over captures/ plus the single write path for capture
artifacts. Loaded once at startup and refreshed on each save, so reads never
re-scan the directory. Wraps persist.save + persist.capture_history."""
from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
import unicodedata
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Optional

from wifit3.crack.hc22000_format import Hc22000Line
from wifit3.crack.wpa_psk import psk_matches, psk_verdict
from wifit3.models import WPS_CAPTURE_TYPES, CaptureType, PersistedCapture
from wifit3.persist import ap_state, save
from wifit3.persist.ap_state import ApState, Key, PasswordStatus, read_hashlines
from wifit3.persist.capture_history import load_capture_index, summarize
from wifit3.persist.common import LEGACY_CAPTURE_RE, bssid_to_dashed, safe_ssid, wps_run_state_name
from wifit3.persist.config import Config
from wifit3.persist.meta import clear_meta_paths_cache, meta_path, update_meta
from wifit3.persist.save import SaveResult
from wifit3.vault.manager import JobManager
from wifit3.wlan.sink import WlanSink

if TYPE_CHECKING:
    from wifit3.models import AccessPoint

logger = logging.getLogger(__name__)

_BAND_SUFFIX_RE = re.compile(r"(?:[\s_-]+(?:2[._]?4|5|6)g(?:hz)?)$", re.IGNORECASE)


def _ssid_family(ssid: str) -> str:
    normalized = unicodedata.normalize("NFKC", ssid).casefold().strip()
    return _BAND_SUFFIX_RE.sub("", normalized).rstrip(" _-")


def _related_ssids(left: str, right: str) -> bool:
    left_family = _ssid_family(left)
    right_family = _ssid_family(right)
    if not left_family or not right_family:
        return False
    if left_family == right_family:
        return True
    shorter, longer = sorted((left_family, right_family), key=len)
    prefixes = tuple(f"{shorter}{separator}" for separator in ("_", "-", " "))
    return len(shorter) >= 4 and longer.startswith(prefixes)


def _sibling_bssids(left: str, right: str) -> bool:
    left_octets = WlanSink.mac_octets(left)
    right_octets = WlanSink.mac_octets(right)
    return (left_octets is not None and right_octets is not None
            and WlanSink.is_sibling(left_octets, right_octets))


def _open_in_file_manager(path: Path) -> None:
    if sys.platform.startswith("win"):
        import os
        os.startfile(path)                                        # noqa: S606 (Windows only)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


@dataclass(frozen=True)
class HashlineSaveResult(SaveResult):
    matches_saved_psk: Optional[bool] = None    # the new hashlines vs the saved PSKs (psk_verdict)


def crosses_auto_crack_threshold(before: List[Hc22000Line], after: List[Hc22000Line]) -> bool:
    """True when a save gives an AP (all its SSIDs) its first line, or its first PMKID / authorized
    EAPOL line after only M1+M2 lines; at most twice per AP."""
    if not before:
        return bool(after)
    return (not any(line.authorized for line in before)
            and any(line.authorized for line in after))


class Vault:
    """Caches the on-disk capture index (from Config.captures_dir) and is the
    backend interface for querying/deleting capture files.
    """

    # Capture-kind -> human label, in display order. The one place these map.
    _KIND_LABELS = {
        CaptureType.HS: "Handshake",
        CaptureType.PMKID: "PMKID",
        CaptureType.WEP: "WEP Key",
        CaptureType.WPS_PIN: "WPS PIN",
        CaptureType.WPS_PBC: "WPS PBC",
        CaptureType.WPA_PSK: "WPA PSK",
    }

    def __init__(self) -> None:
        self._index: Dict[str, List[PersistedCapture]] = {}
        self._ap_states: Dict[str, ApState] = {}
        self._change_listeners: list[Callable[[Optional[str]], None]] = []
        self._probed_ssids_source: Callable[[], Iterable[str]] = lambda: ()
        self._refresh_count = 0
        self._change_counts: Dict[str, int] = {}
        self.errors: list[str] = []                       # surfaced as toasts by the app on mount
        self._propagation_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="psk-propagation")
        try:
            self.refresh()
        except Exception as exc:
            logger.exception("Vault: failed to load the capture index")
            self._index = {}
            self.errors.append(f"Failed to load captures: {exc}")
        self.manager = JobManager(self)
        try:
            self.manager.reconcile_on_startup()
        except Exception as exc:
            logger.exception("Vault: job reconciliation failed on startup")
            self.errors.append(f"Failed to reconcile jobs: {exc}")

    def refresh(self) -> None:
        """Re-scan Config.captures_dir into the cache."""
        clear_meta_paths_cache()
        self._index = load_capture_index()
        self.notify_changed(None)

    def subscribe(self, listener: Callable[[Optional[str]], None]) -> None:
        """``listener`` gets the BSSID of the AP that changed, or None when every AP may have."""
        self._change_listeners.append(listener)

    def set_probed_ssids_source(self, source: Callable[[], Iterable[str]]) -> None:
        """Where cracking jobs read this session's probed SSIDs, tried before the wordlists."""
        self._probed_ssids_source = source

    def add_probed_ssids(self, config: dict) -> dict:
        """``config`` plus this session's probed SSIDs as ``extra_passwords``, when it asks for them."""
        if not config.get("probed_ssid_passwords"):
            return config
        return {**config, "extra_passwords": self.probed_ssids()}

    def probed_ssids(self) -> list[str]:
        """This session's probed SSIDs, sorted."""
        return sorted(self._probed_ssids_source())

    def revision(self, bssid: str) -> tuple[int, int]:
        """Changes whenever the AP's captures or state may have; a cache key for readers."""
        return self._refresh_count, self._change_counts.get(bssid, 0)

    def notify_changed(self, bssid: Optional[str]) -> None:
        """Bump the AP's revision and drop its cached state (every AP's for None), then tell the listeners."""
        if bssid is None:
            self._refresh_count += 1
            self._ap_states.clear()
        else:
            self._change_counts[bssid] = self._change_counts.get(bssid, 0) + 1
            self._ap_states.pop(bssid, None)
        for listener in self._change_listeners:
            try:
                listener(bssid)
            except Exception:
                logger.exception("Vault change listener failed")

    def _insert_capture(self, capture: PersistedCapture) -> None:
        self._index.setdefault(capture.bssid, []).insert(0, capture)
        self.notify_changed(capture.bssid)

    # ----- reads -----

    def persisted(self, bssid: str) -> List[PersistedCapture]:
        """This AP's saved captures, newest-first (empty if none)."""
        return self._index.get(bssid, [])

    def ap_state(self, bssid: str) -> ApState:
        """The AP's keys, hashlines and password status, cached until the AP changes."""
        state = self._ap_states.get(bssid)
        if state is None:
            revision = self.revision(bssid)
            state = self._ap_states[bssid] = ap_state.load_ap_state(bssid, self.persisted(bssid))
            if self.revision(bssid) != revision:    # changed by another thread meanwhile
                self._ap_states.pop(bssid, None)
            self._record_pcap_dates(state)
        return state

    @staticmethod
    def _record_pcap_dates(state: ApState) -> None:
        """Save the dates read from pcaps to the .meta, so they are read once; the state already
        holds them, so nothing is invalidated."""
        by_ssid: Dict[str, Dict[str, tuple[int, ...]]] = {}
        for hashline in state.hashlines:
            if hashline.dated_from_pcaps:
                by_ssid.setdefault(hashline.line.ssid, {})[hashline.id] = hashline.captured
        for ssid, dates in by_ssid.items():
            try:
                ap_state.record_hashline_dates(ssid, state.bssid, dates)
            except OSError:
                logger.warning(f"Could not save hashline dates for {state.bssid}", exc_info=True)

    def ssid(self, bssid: str) -> Optional[str]:
        """The AP's SSID from its newest capture. A PSK is saved under the SSID it cracked, whenever
        it cracked, so PSK files come last; a .pcap only knows the filename-safe form."""
        captures = sorted(self.persisted(bssid), key=lambda c: c.type == CaptureType.WPA_PSK)
        return next((c.ssid for c in captures if c.ssid and not c.path.endswith(".pcap")),
                    next((c.ssid for c in captures if c.ssid), None))

    def all_captures(self) -> List[PersistedCapture]:
        """Every saved capture across all APs, unordered (callers sort as needed)."""
        return [c for caps in self._index.values() for c in caps]

    def capture_payload(self, capture: PersistedCapture) -> str:
        """Copyable text for a capture: its stored value (WEP/WPS), else the file's
        own contents (HS/PMKID hashlines). Empty string if unreadable."""
        if capture.value is not None:
            return capture.value
        try:
            return Path(capture.path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""

    def summary(self) -> Optional[str]:
        """One-line count of every saved capture, e.g. '3 handshakes, 1 WEP key',
        or None when nothing is saved."""
        hs, pmkid, wep, psk = summarize(self._index)
        parts = []
        if hs:
            parts.append(f"{hs} handshake{'s' * (hs != 1)}")
        if pmkid:
            parts.append(f"{pmkid} PMKID{'s' * (pmkid != 1)}")
        if wep:
            parts.append(f"{wep} WEP key{'s' * (wep != 1)}")
        if psk:
            parts.append(f"{psk} PSK{'s' * (psk != 1)}")
        return ", ".join(parts) or None

    def detailed_summary(self, ap: "AccessPoint") -> dict[str, tuple[PersistedCapture, int]]:
        """Per-kind rollup for the Focus 'Existing captures' panel:
        ``{human label: (newest_capture, count)}``, absent kinds omitted, kind order."""
        caps = self.persisted(ap.bssid)
        result: dict[str, tuple[PersistedCapture, int]] = {}
        for kind, label in self._KIND_LABELS.items():
            matching = [c for c in caps if c.type == kind]
            if matching:
                result[label] = (max(matching, key=lambda c: c.timestamp), len(matching))
        return result

    def known_psk(self, ap: "AccessPoint") -> Optional[str]:
        """The passphrase held for this AP: recovered this session (PBC/PIN), else the saved one
        (see ``_saved_psk``). A WPS PIN alone does not count."""
        return ap.wps_pbc_psk or ap.wps_pin_psk or self._saved_psk(ap.bssid)

    def _saved_psk(self, bssid: str) -> Optional[str]:
        """The saved PSK the AP's password status names CURRENT or NOT VERIFIED; a STALE one is
        not held."""
        if not self.persisted(bssid):
            return None
        state = self.ap_state(bssid)
        key = state.status_key
        if key is None or key.type != CaptureType.WPA_PSK:
            return None
        return key.value if state.status in (PasswordStatus.CURRENT, PasswordStatus.NOT_VERIFIED) else None

    def has_psk(self, ap: "AccessPoint") -> bool:
        """True once we hold this AP's passphrase (see known_psk)."""
        return self.known_psk(ap) is not None

    def verify_wpa_psk(self, bssid: str, psk: str) -> Optional[bool]:
        """Check a PSK or 64-hex raw PMK against the AP's saved hashlines (see psk_verdict)."""
        return psk_verdict([psk], [line for _, line in read_hashlines(bssid, self.persisted(bssid)).values()])

    def propagate_verified_psk(self, source_bssid: str, source_ssid: Optional[str],
                               psk: str) -> Future[List[str]]:
        """Save ``psk`` for every related AP (SSID family or sibling BSSID), one propagation at a time on
        a background thread; the future holds their BSSIDs."""
        return self._propagation_executor.submit(self._propagate_verified_psk, source_bssid, source_ssid, psk)

    def _propagate_verified_psk(self, source_bssid: str, source_ssid: Optional[str], psk: str) -> List[str]:
        matched: List[str] = []
        for bssid in list(self._index):
            if bssid == source_bssid:
                continue
            ssid = self.ssid(bssid)
            if not ((source_ssid and ssid and _related_ssids(source_ssid, ssid))
                    or _sibling_bssids(source_bssid, bssid)):
                continue
            if psk in {key.value for key in self.ap_state(bssid).keys}:
                continue
            if any(psk_matches(psk, line) for _, line in read_hashlines(bssid, self.persisted(bssid)).values()):
                self.save_wpa_psk(ssid, bssid, psk)
                matched.append(bssid)
        return matched

    def has_handshake(self, ap: "AccessPoint") -> bool:
        return self._has(ap.bssid, CaptureType.HS)

    def has_pmkid(self, ap: "AccessPoint") -> bool:
        return self._has(ap.bssid, CaptureType.PMKID)

    def has_wep_key(self, ap: "AccessPoint") -> bool:
        return self._has(ap.bssid, CaptureType.WEP)

    def has_wps_psk(self, ap: "AccessPoint") -> bool:
        return any(c.type in WPS_CAPTURE_TYPES and c.value for c in self.persisted(ap.bssid))

    def wps_capture(self, ap: "AccessPoint") -> Optional[PersistedCapture]:
        """Newest saved WPS credential for this AP (PIN- or PBC-derived), or None."""
        return next((c for c in self.persisted(ap.bssid)
                     if c.type in WPS_CAPTURE_TYPES and c.value), None)

    def _has(self, bssid: str, kind: CaptureType) -> bool:
        return any(c.type == kind for c in self.persisted(bssid))

    # ----- writes (persist to disk, then fold into the cache) -----

    def save_handshake(self, ap: "AccessPoint", client_mac: str) -> Optional[HashlineSaveResult]:
        return self._save_hashlines(ap, CaptureType.HS, lambda: save.save_handshake(ap, client_mac))

    def save_pmkid(self, ap: "AccessPoint", client_mac: str) -> Optional[HashlineSaveResult]:
        return self._save_hashlines(ap, CaptureType.PMKID, lambda: save.save_pmkid(ap, client_mac))

    def _save_hashlines(self, ap: "AccessPoint", kind: CaptureType,
                        write: Callable[[], Optional[SaveResult]]) -> Optional[HashlineSaveResult]:
        """Run ``write``; date its new hashlines in the .meta and check them against the saved PSKs.
        Unless one matches, auto-crack the AP's uncracked hashlines (every SSID's)."""
        state = self.ap_state(ap.bssid)
        result = write()
        if not result:
            return None
        update_meta(ap.ssid, ap.bssid)
        if not result.was_new:
            return HashlineSaveResult(result.path, was_new=False)
        capture = PersistedCapture(type=kind, timestamp=int(time.time()), path=str(result.path),
                                   bssid=ap.bssid, ssid=ap.ssid)
        after = read_hashlines(ap.bssid, [*self.persisted(ap.bssid), capture])
        before = {h.id: h.line for h in state.hashlines}
        new_lines = {hid: raw_and_line for hid, raw_and_line in after.items() if hid not in before}
        matches_saved_psk = None
        if new_lines:
            ap_state.record_hashlines_captured(ap.ssid, ap.bssid, [raw for raw, _ in new_lines.values()])
            psks = {k.value for k in state.keys if k.type == CaptureType.WPA_PSK}
            psks |= {psk for psk in (ap.wps_pbc_psk, ap.wps_pin_psk) if psk}
            matches_saved_psk = psk_verdict(psks, [line for _, line in new_lines.values()])
        self._insert_capture(capture)
        if not matches_saved_psk:
            self._auto_crack(ap, state)
        return HashlineSaveResult(result.path, was_new=True, matches_saved_psk=matches_saved_psk)

    def _auto_crack(self, ap: "AccessPoint", before: ApState) -> None:
        """Crack the AP's uncracked hashlines except when the password is CURRENT."""
        tool = self.manager.tools.get(Config.auto_crack) if Config.auto_crack else None
        config = tool.auto_crack_config() if tool is not None else None
        if tool is None or config is None:
            return
        after = self.ap_state(ap.bssid)
        if after.status is PasswordStatus.CURRENT:  # Don't auto-crack known networks
            return
        became_stale = before.status is PasswordStatus.CURRENT and after.status is PasswordStatus.STALE
        if not (crosses_auto_crack_threshold([h.line for h in before.hashlines],
                                             [h.line for h in after.hashlines]) or became_stale):
            return
        uncracked = [h.line for h in after.uncracked]
        if after.hashline_capture is not None and tool.can_crack(uncracked):
            self.manager.submit_job(tool.name, after.hashline_capture, self.add_probed_ssids(config),
                                    auto_crack=True, hashlines=[h.raw for h in after.uncracked])

    def record_live_verification(self, key: Key, correct: bool) -> None:
        """Note that a live check against the AP accepted or rejected the key."""
        ap_state.record_live_verification(key, correct)
        self.notify_changed(key.newest_capture.bssid)

    def save_wep_key(self, ap: "AccessPoint", key: bytes, manual: bool = False) -> Optional[SaveResult]:
        """``manual``: typed in, not recovered from the AP, so unverified until a live check."""
        result = save.save_wep_key(ap, key)
        if result:
            update_meta(ap.ssid, ap.bssid)
        if result and result.was_new:
            if manual:
                ap_state.record_manual_capture(ap.ssid, ap.bssid, result.path)
            self._insert_capture(PersistedCapture(
                type=CaptureType.WEP, timestamp=int(time.time()), path=str(result.path),
                bssid=ap.bssid, value=key.hex(), ssid=ap.ssid))
        return result

    def save_wps_pin(self, ap: "AccessPoint", pin: str, psk: Optional[str],
                     manual: bool = False) -> Optional[SaveResult]:
        """``manual``: typed in, not recovered from the AP, so unverified until a live check."""
        result = save.save_wps_pin(ap, pin, psk)
        if result:
            update_meta(ap.ssid, ap.bssid)
        if result and result.was_new:
            if manual:
                ap_state.record_manual_capture(ap.ssid, ap.bssid, result.path)
            self._insert_capture(PersistedCapture(
                type=CaptureType.WPS_PIN, timestamp=int(time.time()), path=str(result.path),
                bssid=ap.bssid, value=psk, pin=pin, ssid=ap.ssid))
        return result

    def save_wps_pbc(self, ap: "AccessPoint", psk: str) -> Optional[SaveResult]:
        result = save.save_wps_pbc(ap, psk)
        if result:
            update_meta(ap.ssid, ap.bssid)
        if result and result.was_new:
            self._insert_capture(PersistedCapture(
                type=CaptureType.WPS_PBC, timestamp=int(time.time()), path=str(result.path),
                bssid=ap.bssid, value=psk, ssid=ap.ssid))
        return result

    def save_wpa_psk(self, ssid: Optional[str], bssid: str, psk: str, manual: bool = False) -> Optional[SaveResult]:
        """``manual``: typed in, not cracked or recovered from the AP."""
        result = save.save_wpa_psk(ssid, bssid, psk)
        if result:
            update_meta(ssid, bssid)
        if result and result.was_new:
            if manual:
                ap_state.record_manual_capture(ssid, bssid, result.path)
            self._insert_capture(PersistedCapture(
                type=CaptureType.WPA_PSK, timestamp=int(time.time()), path=str(result.path),
                bssid=bssid, value=psk, ssid=ssid))
        return result

    # ----- filesystem ops (the screen goes through these, never touches disk) -----

    def delete_ap(self, bssid: str) -> None:
        """Delete every file of the AP in captures/ (each SSID's captures and .meta, the WPS
        resume file) and drop the cache entries backed by them."""
        captures_dir = Path(Config.captures_dir)
        paths = {Path(c.path) for c in self.persisted(bssid)}
        paths.update(captures_dir.glob(f"*_{bssid_to_dashed(bssid)}[._]*"))
        paths.add(captures_dir / wps_run_state_name(bssid))
        self._delete_files(bssid, paths)

    def delete_key(self, bssid: str, key_id: tuple[CaptureType, str]) -> None:
        """Delete every capture file holding the AP's key ``key_id`` (and any key held only in those
        files), with their live checks and Add key marks."""
        state = self.ap_state(bssid)
        key = next((k for k in state.keys if k.id == key_id), None)
        if key is None:
            return
        paths = {Path(c.path) for c in key.captures}
        removed = [k for k in state.keys if {Path(c.path) for c in k.captures} <= paths]
        try:
            self._delete_files(bssid, paths)
        finally:
            gone = [k for k in removed if not any(Path(c.path).exists() for c in k.captures)]
            for gone_key in gone:
                ap_state.forget_key(gone_key)
            if gone:
                self.notify_changed(bssid)

    def _delete_files(self, bssid: str, paths: set[Path]) -> None:
        """Delete ``paths``, drop the deleted ones from the index and notify every BSSID that lost one;
        re-raises the first failed delete."""
        errors: list[OSError] = []
        try:
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    errors.append(exc)
        finally:
            deleted = {path for path in paths if not path.exists()}
            changed = {bssid}
            for indexed_bssid in list(self._index):
                remaining = [c for c in self._index[indexed_bssid] if Path(c.path) not in deleted]
                if len(remaining) == len(self._index[indexed_bssid]):
                    continue
                changed.add(indexed_bssid)
                if remaining:
                    self._index[indexed_bssid] = remaining
                else:
                    del self._index[indexed_bssid]
            for changed_bssid in changed:
                self.notify_changed(changed_bssid)
        if errors:
            raise errors[0]

    def zip_captures(self, captures: List[PersistedCapture],
                     save_as: Optional[Path] = None) -> Optional[Path]:
        """Zip the given captures' files (deduped) into ``save_as``, defaulting to a
        timestamped archive beside captures/. None if none of the files exist."""
        paths: List[Path] = []
        seen: set[str] = set()
        for p in [Path(c.path) for c in captures] + [meta_path(c.ssid, c.bssid) for c in captures]:
            if p.is_file() and str(p) not in seen:
                seen.add(str(p))
                paths.append(p)
        if not paths:
            return None
        if save_as is None:
            save_as = Path(Config.captures_dir).parent / f"wifit3_captures_{int(time.time())}.zip"
        with zipfile.ZipFile(save_as, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in paths:
                zf.write(p, arcname=p.name)
        return save_as

    def open_directory(self) -> None:
        """Reveal captures/ in the OS file manager; FileNotFoundError before the first capture."""
        captures_dir = Path(Config.captures_dir)
        if not captures_dir.is_dir():
            raise FileNotFoundError(f"No captures dir: {captures_dir}")
        _open_in_file_manager(captures_dir)

    # ----- legacy .hc22000 consolidation (temporary: pre-aggregate installs) ---

    def _legacy_hc_files(self) -> List[Path]:
        """Pre-aggregate per-capture .hc22000 files still on disk (one file per
        capture, vs today's one aggregate file per AP)."""
        root = Path(Config.captures_dir)
        if not root.is_dir():
            return []
        return [p for p in root.iterdir()
                if p.is_file() and (m := LEGACY_CAPTURE_RE.match(p.name))
                and m.group("ext") == "hc22000"]

    def legacy_hc_file_count(self) -> int:
        """Number of legacy per-capture .hc22000 files that could be consolidated."""
        return len(self._legacy_hc_files())

    def legacy_hc_unique_count(self) -> int:
        """Distinct SSID+BSSID the legacy files would collapse into (one file each)."""
        return len({f"{safe_ssid(m.group('ssid'))}_{bssid_to_dashed(m.group('bssid'))}"
                    for p in self._legacy_hc_files()
                    if (m := LEGACY_CAPTURE_RE.match(p.name))})

    def consolidate_legacy_hc_files(self) -> tuple[int, int]:
        """Merge legacy per-capture .hc22000 files into one per AP, then refresh the
        cache. Returns (migrated_ap_count, deleted_file_count)."""
        migrated, deleted = save.consolidate_hc_files(Path(Config.captures_dir))
        self.refresh()
        return migrated, deleted
