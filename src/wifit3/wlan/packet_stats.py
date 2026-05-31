"""Per-(BSSID, class) cumulative frame tallies that feed the Focus live
packet dashboard.

Counts are best-effort and lock-free. RX increments run on the shared
``RxReaderThread`` (the same thread that already bumps ``AccessPoint.beacons``
without a lock); TX increments run on the event loop. A dropped increment is
invisible on a cosmetic meter, so the races aren't worth a lock on the RX hot
path. The dashboard widget reads via :meth:`PacketStats.snapshot` and diffs
successive snapshots into per-window deltas.
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

# Parser frame-type (WlanFrameParser ``type``) → dashboard class. ``qos_data``
# maps to DATA alongside plain ``data`` — almost all modern data frames are QoS,
# so omitting it would leave the data line near-empty. Types not listed here
# (probe_req/resp, assoc_*, mgmt_N, ctrl_*) are intentionally uncounted: it
# keeps the panel to six lines, at the cost of the meter slightly
# under-representing total air time.
_RX_CLASS = {
    "beacon": CLASS_BEACON,
    "data": CLASS_DATA,
    "qos_data": CLASS_DATA,
    "wep_data": CLASS_WEP_IV,
    "eapol": CLASS_EAPOL,
    "deauth": CLASS_DEAUTH,
}


def _zero() -> Dict[str, int]:
    return dict.fromkeys(PACKET_CLASSES, 0)


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
        """Tally an injected frame. Our deauth bursts spike the red DEAUTH
        line (alongside any ambient deauths we hear); every other inject — WEP
        replay/fake-auth, WPS, PMKID, WPA3-down, decloak — spikes INJECT."""
        self._counts[bssid][CLASS_DEAUTH if is_deauth else CLASS_INJECT] += 1

    def snapshot(self, bssid: str) -> Dict[str, int]:
        """Current cumulative counts for ``bssid`` (zero-filled, all classes).
        Returns a copy so a caller can diff it against a later snapshot, and
        never creates a registry entry for an unknown bssid."""
        cur = self._counts.get(bssid)
        return dict(cur) if cur is not None else _zero()
