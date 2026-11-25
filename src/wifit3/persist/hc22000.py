"""Import of hashcat mode 22000 hashlines into the captures directory."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from wifit3.crack.hc22000_format import Hc22000Line, hashline_id, parse_hc22000
from wifit3.dot11.ie import ssid_text
from wifit3.persist.ap_state import record_hashline_dates
from wifit3.persist.config import Config
from wifit3.persist.meta import update_meta
from wifit3.persist.save import HcFiles


@dataclass
class Hc22000Import:
    """Hashlines added to the captures directory, and one ``(filename, problem)`` per line skipped."""
    handshakes: int = 0
    pmkids: int = 0
    duplicates: int = 0
    bssids: set[str] = field(default_factory=set)
    problems: list[tuple[str, str]] = field(default_factory=list)
    dates_by_ap: dict[tuple[str, str], dict[str, list[int]]] = field(default_factory=dict)  # (ESSID hex, BSSID)

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

    def add_hashline(self, hashline: str, entry: Hc22000Line, date: int) -> None:
        """Write the hashline to its AP's .hc22000 unless the Vault already has it."""
        hc = HcFiles(Path(Config.captures_dir), entry.ssid, entry.bssid)
        if entry.is_pmkid:
            existing = hc.find_existing_pmkid(entry.pmkid_or_mic)
        else:
            existing = hc.find_existing_anonce(entry.anonce, entry.authorized)
        if existing is not None:
            self.duplicates += 1
            return
        if entry.is_pmkid:
            hc.write_pmkid(hashline)
            self.pmkids += 1
        else:
            hc.write_handshake([hashline])
            self.handshakes += 1
        self.bssids.add(entry.bssid)
        self.dates_by_ap.setdefault((entry.essid, entry.bssid), {})[hashline_id(hashline)] = [date]

    def write_meta(self) -> None:
        """Record each written hashline's date and SSID bytes in its AP's .meta."""
        for (essid, bssid), dates in self.dates_by_ap.items():
            ssid_bytes = bytes.fromhex(essid)
            ssid = ssid_text(ssid_bytes)
            update_meta(ssid, bssid, ssid_bytes)
            record_hashline_dates(ssid, bssid, dates)
