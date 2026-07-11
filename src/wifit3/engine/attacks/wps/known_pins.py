"""OUI → known default WPS PINs.

Many consumer routers ship a factory WPS PIN that is fixed per hardware family or
computed from the BSSID, so the first six hex of a target's BSSID (its OUI) often
predicts a small set of PINs worth trying *before* the generic COMMON list and the
11k brute sweep. This module maps OUI → those PINs (loaded from ``known_pins.json``).

Provenance / licensing
----------------------
The OUI↔PIN pairs are **factual observations** ("this router family ships / computes
this PIN"), which are not copyrightable (Feist Publications v. Rural Telephone). They
were compiled by the airgeddon project (``known_pins.db``, github.com/v1s1t0r1sh3r3/
airgeddon, GPLv3). Wifite3 is GPLv2 and GPLv3 code cannot be combined with it — so this
is **our own re-expression of the underlying facts** in a JSON data file, credited to
airgeddon as the source of the compilation. It is not a copy of, and does not link,
any airgeddon code. (Data only; the maintainers own this licensing call.)
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Dict, List

_DB_PATH = Path(__file__).parent / "known_pins.json"


@functools.lru_cache(maxsize=1)
def _db() -> Dict[str, List[str]]:
    """The OUI→PINs table, parsed once. Missing/corrupt file → empty (feature off)."""
    try:
        return json.loads(_DB_PATH.read_text())
    except Exception:
        return {}


def known_pins_for(bssid: str) -> List[str]:
    """Known default PINs for a BSSID's OUI (its first 6 hex, any separators), or []."""
    oui = "".join(c for c in bssid if c in "0123456789abcdefABCDEF").upper()[:6]
    return list(_db().get(oui, []))
