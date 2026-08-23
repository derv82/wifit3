"""Load previously-saved captures from captures/ back into per-AP history, so a
recovered key or captured handshake/PMKID re-surfaces as a badge + Focus summary
on the next scan. Classification is by filename; the .pcap companion is skipped
(its hashline sibling carries the verdict). Counterpart: persist.save."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from wifit3.models import PersistedCapture

logger = logging.getLogger(__name__)

# <ssid>_<bssid>_<epoch>_<kind>.<ext>. SSID may itself contain underscores
# ("Basketball_2_4"), so anchor on the dash-separated 6-octet BSSID + epoch +
# kind + extension from the right; the SSID is whatever's left.
_NAME_RE = re.compile(
    r"^(?P<ssid>.+)_"
    r"(?P<bssid>[0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})_"
    r"(?P<epoch>\d+)_"
    r"(?P<kind>handshake|pmkid|wep_key|wps_pin|wps_pbc)"
    r"\.(?P<ext>pcap|hc22000|txt)$"
)

_WEPKEY_RE = re.compile(r"WEP key \(hex\):\s*([0-9a-fA-F]+)")
_WPSPSK_RE = re.compile(r"^PSK:\s*(.+)$", re.MULTILINE)


def _bssid_to_colon(dashed: str) -> str:
    """``aa-bb-cc-dd-ee-ff`` -> ``aa:bb:cc:dd:ee:ff`` (matches AccessPoint.bssid)."""
    return dashed.replace("-", ":").lower()


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("capture_history: unreadable %s: %s", path.name, e)
        return None


def _read_wep_key(path: Path) -> str | None:
    """Extract the hex WEP key from a ``_wep_key.txt`` file, or None."""
    text = _read_text(path)
    if text is None:
        return None
    m = _WEPKEY_RE.search(text)
    return m.group(1).lower() if m else None


def _read_wps_psk(path: Path) -> str | None:
    """Extract the PSK from a ``_wps_pin.txt`` or ``_wps_pbc.txt`` file, or None."""
    text = _read_text(path)
    if text is None:
        return None
    m = _WPSPSK_RE.search(text)
    return m.group(1).strip() if m else None


def _parse_file(path: Path) -> List[PersistedCapture]:
    """Parse one captures/ file into zero or more PersistedCapture entries."""
    m = _NAME_RE.match(path.name)
    if not m:
        return []
    epoch = int(m.group("epoch"))
    kind = m.group("kind")
    ext = m.group("ext")

    if kind == "wep_key" and ext == "txt":
        key = _read_wep_key(path)
        if key is None:
            return []
        return [PersistedCapture(type="WEP", timestamp=epoch,
                                 value=key, path=str(path))]
    if kind in ("wps_pin", "wps_pbc") and ext == "txt":
        return [PersistedCapture(type="WPS", timestamp=epoch,
                                 value=_read_wps_psk(path), path=str(path))]
    if kind == "handshake" and ext == "hc22000":
        return [PersistedCapture(type="HS", timestamp=epoch, path=str(path))]
    if kind == "pmkid" and ext == "hc22000":
        return [PersistedCapture(type="PMKID", timestamp=epoch, path=str(path))]
    # .pcap companion + any other shape: the hashline/text sibling has the verdict.
    return []


def load_capture_index(captures_dir: Path | str = "captures") -> Dict[str, List[PersistedCapture]]:
    """Scan ``captures_dir`` and return {bssid(colon-lower): [PersistedCapture]}.

    Missing directory -> empty index (a fresh install has no history). Entries
    for a BSSID are sorted newest-first.
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


def summarize(index: Dict[str, List[PersistedCapture]]) -> tuple[int, int, int, int]:
    """(handshakes, pmkids, wep_keys, wps_psks) as a count of *APs* that have each
    type, de-duped per AP: an AP with 11 saved handshakes counts as one, not
    eleven."""
    hs = pmkid = wep = wps = 0
    for caps in index.values():
        types = {c.type for c in caps}
        hs += "HS" in types
        pmkid += "PMKID" in types
        wep += "WEP" in types
        wps += "WPS" in types
    return hs, pmkid, wep, wps
