"""WEP fragmentation attack (`aireplay-ng -5`) — M5. SKELETON.

Recovers keystream when there's no client ARP to replay: XOR a captured frame
against the known LLC/SNAP prefix for 8 bytes of keystream, send ≤16 tiny
known-plaintext fragments encrypted with it; the AP reassembles + re-encrypts
under one fresh IV + relays the result, from which we recover a *longer*
keystream (~1500 B). Enough keystream → forge a broadcast ARP → hand to replay.

See README.md "M5/M6 — refined design". wep_crypto.py (forging core) is done +
tested — this just wires the send loop + oracle around it.

Lifecycle mirrors WepArpReplay (start/stop, state, log_callback). The campaign
pauses replay before starting this, and on success this calls back with the
forged ARP so the campaign can resume replay with fresh ammo.

The OFFLINE part (fragment building, keystream extension, ARP forging) is in
wep_crypto.py. The part that needs the live AP is the ORACLE: watching RX for
the AP's relayed reassembled frame to confirm a round worked.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from wifit3.engine.models import AccessPoint

logger = logging.getLogger(__name__)


class WepFragmentation:
    def __init__(
        self,
        iface,
        target: AccessPoint,
        store,
        source_mac: bytes,
        on_forged_arp: Callable[[bytes], None],
        can_inject: Optional[Callable[[], bool]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.iface = iface
        self.target = target
        self.store = store
        self.source_mac = source_mac
        # Called with the forged broadcast-ARP frame when keystream recovery
        # succeeds → campaign feeds it to replay + resumes.
        self._on_forged_arp = on_forged_arp
        self._can_inject = can_inject or (lambda: True)
        self._log = log_callback or (lambda _m: None)
        self.state = "idle"        # idle|seeding|extending|forging|done|failed
        self._active = False

    def start(self) -> None:
        raise NotImplementedError(
            "M5: seed 8B keystream → fragmented sends → extend → forge_arp → "
            "on_forged_arp(); oracle = watch RX for the AP's relayed frame."
        )

    def stop(self):
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active
