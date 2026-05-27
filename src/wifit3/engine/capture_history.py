"""Load previously-saved captures from ``captures/`` into per-AP history.

Wifit3 writes one artifact per capture, named
``<ssid>_<bssid-dashes>_<epoch>[.pcap|.hc22000|_wepkey.txt]`` (see
``hc22000.write_hc22000`` and ``FocusView._save_wep_key``). On scan start we
read that directory back so a previously-cracked WEP key or captured
handshake/PMKID re-surfaces as a badge + a Focus summary, instead of looking
like we have nothing.

The parser is intentionally dumb and read-only: it classifies by *filename*
plus a cheap peek into the ``.hc22000`` text (``WPA*01*`` = PMKID, ``WPA*02*``
= EAPOL handshake). It never parses 802.11 from the ``.pcap`` — the ``.pcap``
is the raw companion to its ``.hc22000`` and is skipped for classification.
Anything that doesn't match the naming scheme (e.g. a bare ``cracks.txt``) is
ignored rather than guessed at.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from wifit3.engine.models import PersistedCapture

logger = logging.getLogger(__name__)

# <ssid>_<bssid>_<epoch>[_wepkey].<ext>. SSID may itself contain underscores
# ("Beachball_2_4"), so anchor on the dash-separated 6-octet BSSID + epoch +
# extension from the right; the SSID is whatever's left.
_NAME_RE = re.compile(
    r"^(?P<ssid>.+)_"
    r"(?P<bssid>[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})_"
    r"(?P<epoch>\d+)"
    r"(?P<wep>_wepkey)?"
    r"\.(?P<ext>pcap|hc22000|txt)$"
)

# "WEP key (hex):   <hex>" line written by FocusView._save_wep_key.
_WEPKEY_RE = re.compile(r"WEP key \(hex\):\s*([0-9a-fA-F]+)")


def _bssid_to_colon(dashed: str) -> str:
    """``f0-af-85-c0-03-0f`` -> ``f0:af:85:c0:03:0f`` (matches AccessPoint.bssid)."""
    return dashed.replace("-", ":").lower()


def _classify_hc22000(path: Path) -> List[str]:
    """Return the capture kinds present in a .hc22000 file (HS and/or PMKID).

    One file can hold both (a PMKID line and an EAPOL line for the same AP), so
    this returns every distinct kind found rather than a single verdict.
    """
    kinds: List[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("capture_history: unreadable %s: %s", path.name, e)
        return kinds
    has_pmkid = has_hs = False
    for line in text.splitlines():
        if line.startswith("WPA*01*"):
            has_pmkid = True
        elif line.startswith("WPA*02*"):
            has_hs = True
    if has_hs:
        kinds.append("HS")
    if has_pmkid:
        kinds.append("PMKID")
    return kinds


def _read_wep_key(path: Path) -> str | None:
    """Extract the hex WEP key from a ``_wepkey.txt`` file, or None."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("capture_history: unreadable %s: %s", path.name, e)
        return None
    m = _WEPKEY_RE.search(text)
    return m.group(1).lower() if m else None


def _parse_file(path: Path) -> List[PersistedCapture]:
    """Parse one captures/ file into zero or more PersistedCapture entries."""
    m = _NAME_RE.match(path.name)
    if not m:
        return []
    epoch = int(m.group("epoch"))
    ext = m.group("ext")
    is_wep = m.group("wep") is not None

    if is_wep and ext == "txt":
        key = _read_wep_key(path)
        if key is None:
            return []
        return [PersistedCapture(kind="WEP", timestamp=epoch,
                                 value=key, path=str(path))]
    if ext == "hc22000":
        return [PersistedCapture(kind=kind, timestamp=epoch, path=str(path))
                for kind in _classify_hc22000(path)]
    # .pcap (raw companion to the .hc22000) and any other non-wepkey .txt: the
    # hc22000/wepkey siblings carry the verdict, so nothing to add here.
    return []


def load_capture_index(captures_dir: Path | str = "captures") -> Dict[str, List[PersistedCapture]]:
    """Scan ``captures_dir`` and return {bssid(colon-lower): [PersistedCapture]}.

    Missing directory -> empty index (a fresh install has no history). Entries
    for a BSSID are sorted newest-first so the Focus summary and any "latest"
    display read naturally.
    """
    index: Dict[str, List[PersistedCapture]] = defaultdict(list)
    root = Path(captures_dir)
    if not root.is_dir():
        return {}
    for path in root.iterdir():
        if not path.is_file():
            continue
        m = _NAME_RE.match(path.name)
        if not m:
            continue
        bssid = _bssid_to_colon(m.group("bssid"))
        index[bssid].extend(_parse_file(path))
    for caps in index.values():
        caps.sort(key=lambda c: c.timestamp, reverse=True)
    return {b: c for b, c in index.items() if c}


def summarize(index: Dict[str, List[PersistedCapture]]) -> tuple[int, int, int]:
    """(handshakes, pmkids, wep_keys) as a count of *APs* that have each kind.

    De-duped per AP for the Scanner summary line: an AP with 11 saved
    handshakes counts as one handshake, not eleven. (The Focus view lists every
    individual artifact — this is only the headline tally.)
    """
    hs = pmkid = wep = 0
    for caps in index.values():
        kinds = {c.kind for c in caps}
        hs += "HS" in kinds
        pmkid += "PMKID" in kinds
        wep += "WEP" in kinds
    return hs, pmkid, wep
