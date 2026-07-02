"""Per-(BSSID, class) cumulative frame tallies that feed the Focus live packet dashboard.

Counts are best-effort and lock-free: RX increments run on the shared ``RxReaderThread``, TX
on the event loop, and a dropped increment is invisible on a cosmetic meter — not worth a
lock on the RX hot path. The widget diffs successive :meth:`snapshot` calls into per-window
deltas.
"""

from collections import defaultdict
from typing import Dict

# Dashboard frame classes, in display (top-to-bottom) order. The widget paints
# one sparkline per entry; the colours/labels live in the widget.
CLASS_BEACON = "beacon"
CLASS_DATA = "data"
CLASS_WEP_IV = "wep_iv"
CLASS_INJECT = "inject"
CLASS_DEAUTH = "deauth"
CLASS_EAPOL = "eapol"

PACKET_CLASSES = (
    CLASS_BEACON,
    CLASS_DATA,
    CLASS_WEP_IV,
    CLASS_INJECT,
    CLASS_DEAUTH,
    CLASS_EAPOL,
)

# Parser frame-type → dashboard class. ``qos_data`` maps to DATA (almost all modern data
# frames are QoS). Unlisted types (probe/assoc/mgmt/ctrl) are intentionally uncounted to keep
# the panel to six lines.
_RX_CLASS = {
    "beacon": CLASS_BEACON,
    "data": CLASS_DATA,
    "qos_data": CLASS_DATA,
    "wep_data": CLASS_WEP_IV,
    "eapol": CLASS_EAPOL,
    "deauth": CLASS_DEAUTH,
}


def _zero() -> Dict[str, int]:
    return {cls: 0 for cls in PACKET_CLASSES}


class PacketStats:
    """Monotonic per-(bssid, class) frame counters."""

    def __init__(self) -> None:
        self._counts: Dict[str, Dict[str, int]] = defaultdict(_zero)

    def record_rx(self, bssid: str, frame_type: str) -> None:
        """Tally a received frame. No-op for frame types the dashboard
        doesn't track (see ``_RX_CLASS``)."""
        cls = _RX_CLASS.get(frame_type)
        if cls is not None:
            self._counts[bssid][cls] += 1

    def record_tx(self, bssid: str, is_deauth: bool) -> None:
        """Tally an injected frame: deauths spike the DEAUTH line (alongside any ambient
        deauths we hear); every other inject spikes INJECT."""
        self._counts[bssid][CLASS_DEAUTH if is_deauth else CLASS_INJECT] += 1

    def snapshot(self, bssid: str) -> Dict[str, int]:
        """Current cumulative counts for ``bssid`` (zero-filled, all classes).
        Returns a copy so a caller can diff it against a later snapshot, and
        never creates a registry entry for an unknown bssid."""
        cur = self._counts.get(bssid)
        return dict(cur) if cur is not None else _zero()
