"""WEP chopchop attack (`aireplay-ng -4`) — M6. SKELETON.

Decrypts one captured frame byte-by-byte using the AP as an ICV oracle, no key:
chop the last byte, guess its plaintext (≤256), fix the ICV (CRC32 is linear —
see wep_crypto.chop_last_byte_and_fixup), send; the AP relays it iff the guess
was right. Walk backwards → full plaintext + keystream → forge a broadcast ARP
→ hand to replay. Slower than fragmentation (256 round-trips/byte); the
fallback when frag gets no response.

See README.md "M5/M6 — refined design". Build wep_crypto.py + its tests FIRST.

Lifecycle mirrors WepArpReplay (start/stop, state, log_callback). Campaign
pauses replay first; on success this calls back with the forged ARP.

OFFLINE part (the ICV fix-up + ARP forging) is in wep_crypto.py. The ORACLE —
deciding whether a guess was accepted by watching RX for the AP relaying the
shortened frame — needs the live AP.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from wifit3.engine.models import AccessPoint

logger = logging.getLogger(__name__)


class WepChopChop:
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
        self._on_forged_arp = on_forged_arp
        self._can_inject = can_inject or (lambda: True)
        self._log = log_callback or (lambda _m: None)
        self.state = "idle"        # idle|chopping|forging|done|failed
        self._bytes_done = 0       # progress for the UI (of ~36+ to decrypt)
        self._active = False

    def start(self) -> None:
        raise NotImplementedError(
            "M6: per byte, try ≤256 guesses (chop_last_byte_and_fixup) against "
            "the AP oracle → recover plaintext/keystream → forge_arp → "
            "on_forged_arp(). Oracle = watch RX for the relayed shortened frame."
        )

    def stop(self):
        self._active = False

    @property
    def is_active(self) -> bool:
        return self._active
