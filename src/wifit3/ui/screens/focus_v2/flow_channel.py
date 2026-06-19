"""The packet-flow channel — the centerpiece.

A multi-row sparkline meter the v1 ``PacketDashboard``'s single-series paint
can't express: N labelled rows, per-row colour, a trailing ``/s`` rate (or a
recent count), all flowing **right->left** (newest sample at the right edge,
history scrolling left — you read the attack's recent past L->R). Height is
**adaptive**: 2-row (16 levels) when there's vertical room, 1-row (8 levels)
when cramped — the same "shrink gracefully" rule the bottom band rides.

Data-source agnostic: once :meth:`FlowChannel.reconfigure` binds an interface +
BSSID, ``_tick`` samples ``WlanInterface.packet_stats`` deltas (each row's key
matches a ``wlan.packet_stats`` class). Unbound (the geometry tests, the
``shoot_focus_v2`` screenshots), it falls back to a lively fake generator so the
look can still be judged.
"""
from __future__ import annotations

import math
from collections import deque

from rich.text import Text
from textual.widgets import Static

# " ▁▂▃▄▅▆▇█": index 0 is blank, used only for the upper row of a 2-row pair.
# The lower row and the 1-row sparkline floor at ▁ (index 1), so a zero/quiet row
# reads as a continuous flat line end-to-end rather than an empty gap.
_BLOCKS = " ▁▂▃▄▅▆▇█"
_LABEL_W = 6
_NUM_W = 5
_GUTTER = _LABEL_W + 1 + 1 + _NUM_W      # label + space + space + number
_HISTORY = 256


class FlowChannel(Static):
    SAMPLE_S = 0.4                        # fake-sample cadence; ~window for the rate

    def __init__(self, rows, **kwargs) -> None:
        super().__init__(**kwargs)
        self._rows = rows                 # list[FlowRow]
        self._hist = {r.key: deque([0] * _HISTORY, maxlen=_HISTORY) for r in rows}
        self._t = 0
        # Live binding (None -> fake generator). _prev is the last cumulative
        # packet_stats snapshot, diffed each tick into a per-window delta.
        self._iface = None
        self._bssid = None
        self._prev = None
        # Optional centered footer lines painted in the channel's own vertical
        # slack below the sparklines (the WEP fake-auth + usable-IV status), so
        # they cost no row from the LOG/CLIENTS bands. None/[] -> no footer.
        self._footer = None

    def on_mount(self) -> None:
        self.set_interval(self.SAMPLE_S, self._tick)
        self._tick()

    def on_resize(self) -> None:
        self._repaint()

    # ---- target binding -----------------------------------------------------

    def reconfigure(self, rows, iface, bssid) -> None:
        """Point the channel at a target: swap in the family's rows (WEP shows
        wep_iv, WPA eapol), bind the interface + BSSID, and clear history so a
        previous target's bars never bleed in. ``iface``/``bssid`` may be None,
        which drops back to the fake generator."""
        self._rows = rows
        self._hist = {r.key: deque([0] * _HISTORY, maxlen=_HISTORY) for r in rows}
        self._iface = iface
        self._bssid = bssid
        self._prev = (iface.packet_stats.snapshot(bssid)
                      if (iface is not None and bssid) else None)
        self._footer = None              # cleared per target; the screen re-sets it for WEP
        self._repaint()

    def set_footer(self, lines) -> None:
        """Set (or clear, with None/[]) the centered footer lines below the
        sparklines. ``lines`` is a list of rich Texts the caller has rendered."""
        self._footer = lines or None
        self._repaint()

    # ---- sampling -----------------------------------------------------------

    def _tick(self) -> None:
        if self._iface is not None and self._bssid is not None:
            self._sample_live()
        else:
            self._sample_fake()
        self._repaint()

    def _sample_live(self) -> None:
        snap = self._iface.packet_stats.snapshot(self._bssid)
        if self._prev is not None:
            for r in self._rows:
                # max(0, …) guards a fresh rebind where prev briefly out-runs snap.
                self._hist[r.key].append(max(0, snap.get(r.key, 0) - self._prev.get(r.key, 0)))
        self._prev = snap

    def _sample_fake(self) -> None:
        self._t += 1
        for r in self._rows:
            # Per-window counts scaled so the trailing /s reads near the row's
            # nominal peak; a periodic burst makes inject/deauth/eapol pulse.
            wobble = 0.8 + 0.4 * math.sin(self._t / 4.0 + (hash(r.key) % 7))
            sample = r.peak * self.SAMPLE_S * wobble
            if r.key in ("inject", "deauth", "eapol") and (self._t % 11) in (0, 1):
                sample += r.peak * self.SAMPLE_S * 0.8
            self._hist[r.key].append(max(0, int(round(sample))))

    # ---- paint --------------------------------------------------------------

    def _bar_width(self) -> int:
        return max(4, (self.content_size.width or 50) - _GUTTER)

    def _two_row(self) -> bool:
        h = self.content_size.height or 12
        return h // max(1, len(self._rows)) >= 2

    def _col(self, v: int, peak: int, levels: int) -> int:
        if v <= 0:
            return 0
        return max(1, min(levels, round(v / peak * levels)))

    def _repaint(self) -> None:
        bw = self._bar_width()
        two = self._two_row()
        lines: list[Text] = []
        for r in self._rows:
            window = list(self._hist[r.key])[-bw:]
            peak = max(max(window, default=0), 1)
            recent = list(self._hist[r.key])[-6:]
            avg = sum(recent) / (len(recent) * self.SAMPLE_S) if recent else 0
            num = f"{avg:.0f}/s" if r.as_rate else str(sum(recent))
            # A flat row (no datapoint in the drawn window) is dimmed whole — label,
            # bars, rate — so quiet rows (inject/deauth/eapol when idle) recede
            # instead of competing for attention. Keeps the colour, drops the shout.
            color = r.color if any(window) else f"dim {r.color}"

            if two:
                upper = "".join(_BLOCKS[max(0, self._col(v, peak, 16) - 8)] for v in window)
                lower = "".join(_BLOCKS[max(1, min(8, self._col(v, peak, 16)))] for v in window)
                lines.append(self._row("", color, upper, "", bw))
                lines.append(self._row(r.label, color, lower, num, bw))
            else:
                cells = "".join(_BLOCKS[max(1, self._col(v, peak, 8))] for v in window)
                lines.append(self._row(r.label, color, cells, num, bw))
        # WEP status footer lines sit a blank line below the sparklines, each
        # centered in the channel width — they live in the block's own vertical
        # slack so they cost no row from the LOG/CLIENTS bands below.
        if self._footer is not None:
            w = self.content_size.width or 50
            lines.append(Text(""))
            for fl in self._footer:
                lead = max(0, (w - fl.cell_len) // 2)
                lines.append(Text(" " * lead) + fl)
        # Vertically centre the block so it lines up with the vertically-centred
        # card/router columns as the band grows.
        h = self.content_size.height or len(lines)
        pad = max(0, (h - len(lines)) // 2)
        self.update(Text("\n").join([Text("")] * pad + lines))

    def _row(self, label: str, color: str, cells: str, num: str, bw: int) -> Text:
        """Right-aligned label · space · bars (right-aligned, newest at the right)
        · space · left-aligned number — label and number sit flush against the
        bars. Blank label/number on the upper row of a 2-row pair."""
        t = Text()
        t.append(f"{label:>{_LABEL_W}} ", style=color if label else "")
        t.append(cells.rjust(bw), style=color)
        t.append(" ")
        t.append(f"{num:<{_NUM_W}}", style=color if num else "")
        return t
