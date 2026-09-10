"""Load previously-saved captures from captures/ back into per-AP history, so a
recovered key or captured handshake/PMKID re-surfaces as a badge + Focus summary
on the next scan. Classification is by filename; the .pcap companion is skipped
(its hashline sibling carries the verdict). Counterpart: persist.save."""
from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from wifit3.models import PersistedCapture
from wifit3.persist.common import (
    AGGREGATED_HC22000_RE,
    LEGACY_CAPTURE_RE,
    WEP_KEY_HEX_RE,
    WPS_PSK_RE,
    bssid_to_colon,
)
from wifit3.persist.config import Config

logger = logging.getLogger(__name__)


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
    m = WEP_KEY_HEX_RE.search(text)
    return m.group(1).lower() if m else None


def _read_wps_psk(path: Path) -> str | None:
    """Extract the PSK from a ``_wps_pin.txt`` or ``_wps_pbc.txt`` file, or None."""
    text = _read_text(path)
    if text is None:
        return None
    m = WPS_PSK_RE.search(text)
    return m.group(1).strip() if m else None


def _parse_aggregate_hc(path: Path) -> List[PersistedCapture]:
    text = _read_text(path)
    if text is None:
        return []
    try:
        mtime = int(path.stat().st_mtime)
    except OSError:
        mtime = 0
    has_pmkid = False
    has_hs = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("WPA*01*"):
            has_pmkid = True
        elif line.startswith("WPA*02*"):
            has_hs = True
    out: List[PersistedCapture] = []
    if has_pmkid:
        out.append(PersistedCapture(type="PMKID", timestamp=mtime, path=str(path)))
    if has_hs:
        out.append(PersistedCapture(type="HS", timestamp=mtime, path=str(path)))
    return out


def _parse_file(path: Path) -> List[PersistedCapture]:
    """Parse one captures/ file into zero or more PersistedCapture entries."""
    if AGGREGATED_HC22000_RE.match(path.name):
        return _parse_aggregate_hc(path)

    m = LEGACY_CAPTURE_RE.match(path.name)
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


def load_capture_index() -> Dict[str, List[PersistedCapture]]:
    """Scan ``captures_dir``, return bssid->captures sorted by newest-first."""
    index: Dict[str, List[PersistedCapture]] = defaultdict(list)
    root = Path(Config.captures_dir)
    if not root.is_dir():
        return {}
    for path in root.iterdir():
        if not path.is_file():
            continue
        m = LEGACY_CAPTURE_RE.match(path.name)
        if m:
            bssid = bssid_to_colon(m.group("bssid"))
            index[bssid].extend(_parse_file(path))
            continue
        m_agg = AGGREGATED_HC22000_RE.match(path.name)
        if m_agg:
            bssid = bssid_to_colon(m_agg.group("bssid"))
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
