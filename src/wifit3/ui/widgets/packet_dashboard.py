"""Live packet dashboard — per-class sparklines for the focused AP.

Samples ``WlanInterface.packet_stats`` on a timer, diffs successive cumulative
snapshots into per-window deltas, and paints one colour-coded scrolling line
per frame class. Read-only on the wire — it only reads counters other code
already maintains, so it adds no 802.11 traffic.

The WEP-IV and EAPOL rows are encryption-gated to the focused target: WEP IVs
only matter on a WEP AP, EAPOL only on a WPA/WPA2/WPA3 AP (see ``focus_on``),
so at most one of the two ever shows (and neither on an OPEN AP).

Scale is per-line ("breathing"): each sparkline autoscales to its own recent peak, so a 1/s
class and a 40/s class both fill the height and stay legible. The trailing number is the
absolute volume — a ``/s`` rate for continuous classes, a recent count for bursty ones.

The "PACKET ACTIVITY" title is a sibling ``.panel-title`` Label (built in
FocusViewV2.compose), not painted here — so it reads as the same bar as the
SECURITY / CAPTURE panels.
"""

# TODO: Textual 0.27+ has Sparklines built in! And they are prettier
# https://textual.textualize.io/widgets/sparkline/
# from textual.widgets import Sparkline

from collections import deque
from typing import Dict, Iterator, Optional

from rich.text import Text
from textual.widgets import Static

from wifit3.wlan.packet_stats import PACKET_CLASSES

# Block-eighths. Index 0 (▁) is the always-on baseline: an empty column reads
# as a continuous dim floor, not a gap, so a quiet row still looks like a row.
# Any non-zero count pokes at least one step above it (see _repaint).
_SPARK = "▁▂▃▄▅▆▇█"
_LEVELS = len(_SPARK) - 1  # 7 steps above the baseline

# class → (label ≤6 chars, colour, show_as_rate). Colours are picked so no two
# *simultaneously visible* rows collide — wep_iv and eapol never show together
# (encryption-gated), so they share green as the "key material" colour. beacon
# is cyan (not blue) so it stays distinct from data, the adjacent row.
# Continuous classes read as a /s rate; bursty events read as a recent count.
_CLASS_STYLE = {
    "beacon": ("beacon", "cyan", True),
    "data": ("data", "blue", True),
    "wep_iv": ("wep iv", "green", True),
    "inject": ("inject", "orange1", True),
    "deauth": ("deauth", "red", False),
    "eapol": ("eapol", "green", False),
}

_LABEL_W = 6          # widest label ("beacon", "wep iv")
_NUM_W = 7            # trailing rate/count field, right-aligned


class PacketDashboard(Static):
    """Per-class sparkline meter for one (interface, BSSID)."""

    # 0.5 s/sample keeps the lines lively; the 3 s rate window is 6 samples.
    SAMPLE_S = 0.5
    HISTORY = 64                              # ring length; render slices the tail
    _RATE_WINDOW = int(3.0 / SAMPLE_S)       # samples folded into the /s rate

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._iface = None
        self._bssid: Optional[str] = None
        self._show_wep = False               # gate the WEP-IV row (WEP targets)
        self._show_eapol = False             # gate the EAPOL row (WPA targets)
        self._prev: Optional[Dict[str, int]] = None      # last cumulative snapshot
        self._hist: Dict[str, deque] = {
            c: deque([0] * self.HISTORY, maxlen=self.HISTORY) for c in PACKET_CLASSES
        }
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_interval(self.SAMPLE_S, self._sample)
        self._repaint()

    def on_resize(self) -> None:
        # Sparkline width tracks the panel width — repaint with the new geometry.
        self._repaint()

    # ----- target binding ----------------------------------------------------

    def focus_on(
        self, iface, bssid: Optional[str], *,
        show_wep: bool = False, show_eapol: bool = False,
    ) -> None:
        """Point the dashboard at a target and clear history. ``iface`` may be
        None (no card) — the panel then renders idle/dim. ``show_wep`` /
        ``show_eapol`` gate the encryption-specific rows to the target's family.
        Called on every Focus target acquisition so windows never bleed across
        targets."""
        self._iface = iface
        self._bssid = bssid
        self._show_wep = show_wep
        self._show_eapol = show_eapol
        self._prev = (
            iface.packet_stats.snapshot(bssid) if iface and bssid else None
        )
        for d in self._hist.values():
            d.clear()
            d.extend([0] * self.HISTORY)
        self._repaint()

    def set_gates(self, *, show_wep: bool, show_eapol: bool) -> None:
        """Update only the encryption-gated rows, without clearing history.
        Cheap enough to call every UI tick — a target's encryption label can
        still upgrade after focus (provisional WEP → WPA2 on a weak radio), so
        the gating is re-evaluated live rather than pinned at focus time."""
        if (show_wep, show_eapol) != (self._show_wep, self._show_eapol):
            self._show_wep = show_wep
            self._show_eapol = show_eapol
            self._repaint()

    # ----- sampling ----------------------------------------------------------

    def _sample(self) -> None:
        if not self._iface or not self._bssid:
            return
        snap = self._iface.packet_stats.snapshot(self._bssid)
        if self._prev is not None:
            for c in PACKET_CLASSES:
                # max(0, …) guards the focus_on reset, where prev briefly
                # out-runs a fresh snapshot.
                self._hist[c].append(max(0, snap[c] - self._prev[c]))
        self._prev = snap
        self._repaint()

    # ----- paint -------------------------------------------------------------

    def _visible_classes(self) -> Iterator[str]:
        """Class rows to draw, in order — minus the encryption rows that don't
        apply to the focused target (WEP-IV off WEP, EAPOL off WPA)."""
        for c in PACKET_CLASSES:
            if c == "wep_iv" and not self._show_wep:
                continue
            if c == "eapol" and not self._show_eapol:
                continue
            yield c

    def _spark_width(self) -> int:
        avail = self.content_size.width
        if avail <= 0:
            avail = 50                       # pre-layout fallback
        return avail - _LABEL_W - 1 - _NUM_W

    def _repaint(self) -> None:
        active = self._iface is not None and self._bssid is not None
        spark_w = self._spark_width()
        lines = []

        for cls in self._visible_classes():
            label, colour, show_rate = _CLASS_STYLE[cls]
            hist = self._hist[cls]
            window = list(hist)[-spark_w:] if spark_w > 0 else []
            peak = max(window) if window else 0
            idle = (not active) or peak == 0
            style = "dim" if idle else colour

            if window and peak > 0:
                # v==0 → baseline; any activity pokes ≥1 step up so a single
                # frame against a tall peak is still visible (not rounded away).
                spark = "".join(
                    _SPARK[max(1, round(v / peak * _LEVELS))] if v > 0
                    else _SPARK[0]
                    for v in window
                )
            else:
                # Match the data-row width (capped at HISTORY by the tail slice) so an empty
                # row doesn't outrun the data rows and misalign the numbers.
                spark = _SPARK[0] * max(0, min(spark_w, len(hist)))

            # Number reads the recent (~3 s) window so a deauth burst's count
            # decays a few seconds after it stops rather than lingering for the
            # whole visible history.
            recent = list(hist)[-self._RATE_WINDOW:]
            num = f"{sum(recent) / 3.0:.0f}/s" if show_rate else str(sum(recent))

            line = Text()
            line.append(f"{label:<{_LABEL_W}} ", style=style)
            line.append(spark, style=style)
            line.append(f"{num:>{_NUM_W}}", style=style)
            lines.append(line)

        self.update(Text("\n").join(lines) if lines else Text(""))
