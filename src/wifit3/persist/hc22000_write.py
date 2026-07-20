"""Write hashcat -m 22000 hashlines for an AP to disk (the persistence half of hc22000)."""
from __future__ import annotations

from pathlib import Path

from wifit3.models import AccessPoint
from wifit3.crack.hc22000_format import format_ap_hashlines


def write_hc22000(path: Path, ap: AccessPoint) -> int:
    """Write all hashlines for *ap* to *path*. Returns the count written; writes
    nothing (and returns 0) if none could be produced."""
    lines = format_ap_hashlines(ap)
    if not lines:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)
