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

import logging
from typing import Callable, Optional

from wifit3.engine.models import AccessPoint
from wifit3.engine.attacks.wep.fake_auth import WepFakeAuth
from wifit3.engine.attacks.wep.arp_replay import WepArpReplay

logger = logging.getLogger(__name__)


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
        logger.info("[WEP-Campaign] Started on %s", self.target.bssid)

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        # Stop replay (TX) before fake-auth so we don't briefly inject while
        # de-registering the forged STA.
        self.replay.stop()
        self.fake_auth.stop()
        self._log("[bold red]✗ Generate IVs stopped.[/bold red]")
        logger.info("[WEP-Campaign] Stopped on %s", self.target.bssid)

    @property
    def is_active(self) -> bool:
        return self._active
