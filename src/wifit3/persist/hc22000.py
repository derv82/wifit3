"""Import of hashcat mode 22000 hashlines into the captures directory."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from wifit3.crack.hc22000_format import Hc22000Line, hashline_id, parse_hc22000, read_hc22000
from wifit3.dot11.ie import ssid_text
from wifit3.persist.ap_state import record_hashline_dates
from wifit3.persist.common import bssid_to_dashed
from wifit3.persist.config import Config
from wifit3.persist.meta import update_meta
from wifit3.persist.save import HcFiles

DASHED_BSSID_RE = re.compile(r"[0-9a-f]{2}(?:-[0-9a-f]{2}){5}")


@dataclass
class Hc22000Import:
    """Hashlines added to the captures directory, and one ``(filename, problem)`` per line skipped."""
    handshakes: int = 0
    pmkids: int = 0
    duplicates: int = 0
    bssids: set[str] = field(default_factory=set)
    problems: list[tuple[str, str]] = field(default_factory=list)
    new_hashlines_by_ap: dict[tuple[str, str], list[tuple[str, int]]] = field(default_factory=dict)  # (ESSID hex, BSSID)
    saved_by_path: dict[Path, tuple[dict[str, bool], set[str]]] = field(default_factory=dict)  # ANonces, PMKIDs
    paths_by_bssid: Optional[dict[str, list[Path]]] = None     # the captures dir, listed once, by dashed BSSID

    def add_file(self, filename: str, text: str, file_date: int) -> None:
        """Add each line, dated ``file_date``; anything after a tab (``--add-timestamp``) is dropped."""
        for number, raw in enumerate(text.splitlines(), start=1):
            hashline = raw.strip().partition("\t")[0]
            if not hashline:
                continue
            entry = parse_hc22000(hashline)
            if entry is None:
                self.problems.append((f"{filename}:{number}", "not a hashline"))
                continue
            if not entry.essid:
                self.problems.append((f"{filename}:{number}", "no SSID"))
                continue
            self.add_hashline(hashline, entry, file_date)

    def saved_paths(self, bssid: str, suffix: str) -> list[Path]:
        """The captures directory's files of the BSSID ending in ``suffix``, as listed at the first call."""
        if self.paths_by_bssid is None:
            self.paths_by_bssid = {}
            captures_dir = Path(Config.captures_dir)
            if captures_dir.is_dir():
                for path in captures_dir.iterdir():
                    dashed_bssids = DASHED_BSSID_RE.findall(path.name.lower())
                    if dashed_bssids:
                        self.paths_by_bssid.setdefault(dashed_bssids[-1], []).append(path)
        return [path for path in self.paths_by_bssid.get(bssid_to_dashed(bssid), [])
                if path.name.lower().endswith(suffix)]

    def add_hashline(self, hashline: str, entry: Hc22000Line, date: int) -> None:
        """Queue the hashline for its SSID's .hc22000 unless that file, the AP's split files or this import
        already has its PMKID, or its ANonce at least as strong (an M1+M2 doesn't count against an M2+M3)."""
        agg_path = HcFiles(Path(Config.captures_dir), entry.ssid, entry.bssid).agg_path
        if agg_path not in self.saved_by_path:
            saved_anonces: dict[str, bool] = {}
            saved_pmkids: set[str] = set()
            paths = [agg_path] if agg_path.exists() else []
            paths += self.saved_paths(entry.bssid, "_handshake.hc22000") + self.saved_paths(entry.bssid, "_pmkid.hc22000")
            for path in paths:
                for _, line in read_hc22000(path):
                    if line.is_pmkid:
                        saved_pmkids.add(line.pmkid_or_mic)
                    elif line.anonce:
                        saved_anonces[line.anonce] = saved_anonces.get(line.anonce, False) or line.authorized
            self.saved_by_path[agg_path] = (saved_anonces, saved_pmkids)
        anonces, pmkids = self.saved_by_path[agg_path]
        if entry.is_pmkid:
            if entry.pmkid_or_mic in pmkids:
                self.duplicates += 1
                return
            pmkids.add(entry.pmkid_or_mic)
            self.pmkids += 1
        else:
            if entry.anonce in anonces and (anonces[entry.anonce] or not entry.authorized):
                self.duplicates += 1
                return
            anonces[entry.anonce] = anonces.get(entry.anonce, False) or entry.authorized
            self.handshakes += 1
        self.bssids.add(entry.bssid)
        self.new_hashlines_by_ap.setdefault((entry.essid, entry.bssid), []).append((hashline, date))

    def write_files(self) -> None:
        """Append each AP's new hashlines to its .hc22000, and record their dates and its SSID bytes in its .meta."""
        captures_dir = Path(Config.captures_dir)
        captures_dir.mkdir(parents=True, exist_ok=True)
        for (essid, bssid), new_hashlines in self.new_hashlines_by_ap.items():
            ssid_bytes = bytes.fromhex(essid)
            ssid = ssid_text(ssid_bytes)
            with HcFiles(captures_dir, ssid, bssid).agg_path.open("a", encoding="utf-8") as f:
                f.write("\n".join(hashline for hashline, _ in new_hashlines) + "\n")
            update_meta(ssid, bssid, ssid_bytes)
            record_hashline_dates(ssid, bssid, {hashline_id(hashline): [date] for hashline, date in new_hashlines})
