"""Deferred, per-client aggregation of EAPOL frames into one handshake tree.

The granular capture detector surfaces every EAPOL frame the instant it lands;
logged raw that's a wall of near-duplicate ``M1 …`` / ``M1 …`` / ``M3 …`` lines
during a deauth storm. This aggregator buffers a client's frames and emits one
tidy tree instead::

    4-Way Handshake · Client: aa:bb:cc:dd:ee:ff
     ├─► M1 ×20 (AP→Client) ANonce✓ PMKID✓
     ├─► M2     (AP←Client) SNonce✓ MIC✓ EAPOL✓
     └─► ✓ Valid 4-Way Handshake (M1+M2)

Flush policy (chosen with the user):

* **First crackable pair → flush immediately.** The win shows the instant the
  handshake is crackable, usually at M2, *before* M4; we don't even keep M4.
* **Then suppress that client until a new ANonce instance completes.** The
  detector fires one HANDSHAKE event per instance, so retransmitted M3/M4 of the
  captured exchange are swallowed; a genuine re-handshake re-announces (``×2``).
* **Partial bursts** (frames that never complete) flush after ``settle_s`` of
  quiet with no verdict leaf, so a client spamming M1 is still surfaced once.

Pure + clock-injected (``now`` is passed in), so it unit-tests with no UI / no
hardware. Side effects (saving the .pcap) stay in the screen.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..campaigns import treelog
from .capture_events import CaptureEvent
from .capture_log import _eapol_fields

# AP is always drawn on the left; the arrow shows the frame's direction.
_DIR = {1: "AP→Client", 3: "AP→Client", 2: "AP←Client", 4: "AP←Client"}


def _field_score(ev: CaptureEvent) -> int:
    """How 'complete' a frame is, used to pick the representative when a message
    is aggregated, so ``M1 ×20`` shows the best M1 we actually saw (not a synthetic
    OR of fields across frames)."""
    return sum(bool(x) for x in (ev.has_nonce, ev.has_mic, ev.eapol_complete, ev.has_pmkid))


@dataclass
class _MsgAgg:
    rep: CaptureEvent          # most-complete representative frame for this Mx
    count: int = 1

    def merge(self, ev: CaptureEvent) -> None:
        self.count += 1
        if _field_score(ev) > _field_score(self.rep):
            self.rep = ev


@dataclass
class _Burst:
    last_ts: float = 0.0
    msgs: dict = field(default_factory=dict)        # msg_num -> _MsgAgg

    def add(self, ev: CaptureEvent, now: float) -> None:
        self.last_ts = now
        m = ev.msg_num or 0
        agg = self.msgs.get(m)
        if agg is None:
            self.msgs[m] = _MsgAgg(rep=ev)
        else:
            agg.merge(ev)


def _msg_label(msg_num: int, count: int) -> str:
    base = f"M{msg_num}" if msg_num else "EAPOL?"
    return f"{base} ×{count}" if count > 1 else base


def _render_tree(client_mac: str, burst: _Burst, hs_ev, instance: int,
                 save_hint: str | None = None) -> list[str]:
    """A burst (+ optional completion event + save note) as treelog markup lines.

    ``save_hint`` (the screen's short 'saved: captures/…' string) closes the tree
    as its terminal leaf, so the save note lives *inside* the handshake tree
    rather than as a stray left-aligned line below it."""
    header = f"[bold]4-Way Handshake[/bold] [dim]·[/dim] Client: [bold]{client_mac}[/bold]"
    nums = sorted(burst.msgs, key=lambda m: (m == 0, m))   # M1..M4, unclassified last
    label_w = max((len(_msg_label(m, burst.msgs[m].count)) for m in nums), default=0)
    bodies = []
    for m in nums:
        agg = burst.msgs[m]
        label = _msg_label(m, agg.count).ljust(label_w)
        fields = " ".join(_eapol_fields(agg.rep))
        bodies.append(f"{label} ({_DIR.get(m, '?')}) {fields}".rstrip())

    lines = [header]
    if hs_ev is not None:
        verdict = (f"[black bold on green] ✓ Valid 4-Way Handshake "
                   f"({hs_ev.pair_label}) [/black bold on green]")
        if instance > 1:
            verdict += f" [dim](capture ×{instance})[/dim]"
        lines += [treelog.branch(b) for b in bodies]       # every Mx a branch …
        if save_hint:                                      # … verdict branches too,
            lines.append(treelog.branch(verdict))          #     the save note closes
            lines.append(treelog.leaf(save_hint))
        else:
            lines.append(treelog.leaf(verdict))            # … else verdict closes it
    else:                                                  # partial: no verdict
        for i, body in enumerate(bodies):
            connector = treelog.leaf if i == len(bodies) - 1 else treelog.branch
            lines.append(connector(body))
    return lines


def _render_reannounce(client_mac: str, hs_ev, instance: int) -> list[str]:
    """A compact one-liner for a repeat capture on an already-announced client
    (its per-frame detail was suppressed, so there's no tree to redraw)."""
    return [f"[black bold on green] ✓ Valid 4-Way Handshake ({hs_ev.pair_label}) "
            f"[/black bold on green] [dim](capture ×{instance})[/dim] "
            f"[bold]{client_mac}[/bold]"]


class EapolAggregator:
    """Buffers EAPOL frames per client and renders handshake trees on a deferred
    schedule. The screen feeds it events + ``now`` and logs whatever it returns;
    :meth:`tick` must be called each UI tick to flush settled partials."""

    def __init__(self, settle_s: float = 3.0) -> None:
        self._settle = settle_s
        self._bursts: dict[str, _Burst] = {}
        self._captured: set[str] = set()        # clients whose HS already announced
        self._instances: dict[str, int] = {}    # client -> # instances announced

    def reset(self) -> None:
        """Drop all state: on a target switch (the new AP starts fresh)."""
        self._bursts.clear()
        self._captured.clear()
        self._instances.clear()

    def on_eapol(self, ev: CaptureEvent, now: float) -> None:
        """Buffer a new EAPOL frame, unless its client is already captured (then
        we suppress until a new instance completes)."""
        if ev.client_mac in self._captured:
            return
        self._bursts.setdefault(ev.client_mac, _Burst()).add(ev, now)

    def on_handshake(self, ev: CaptureEvent, now: float,
                     save_hint: str | None = None) -> list[str]:
        """A crackable pair completed (one HANDSHAKE per instance). First time for
        a client → flush its buffered tree with the verdict (and the screen's
        ``save_hint`` as the closing leaf); afterwards → a compact ``×N``
        re-announce. Returns the markup lines to log now."""
        n = self._instances.get(ev.client_mac, 0) + 1
        self._instances[ev.client_mac] = n
        if ev.client_mac not in self._captured:
            self._captured.add(ev.client_mac)
            burst = self._bursts.pop(ev.client_mac, _Burst())
            return _render_tree(ev.client_mac, burst, ev, n, save_hint)
        return _render_reannounce(ev.client_mac, ev, n)

    def tick(self, now: float) -> list[list[str]]:
        """Flush per-client bursts quiet for >= ``settle_s`` (partials, no
        verdict). Returns a list of trees (each a list of markup lines)."""
        out = []
        for mac in list(self._bursts):
            if now - self._bursts[mac].last_ts >= self._settle:
                out.append(_render_tree(mac, self._bursts.pop(mac), None, 0))
        return out
