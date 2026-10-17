"""802.11 open-auth + association against one AP, plus a live-interface transport.

``WlanTransport`` adapts a ``WlanInterface`` to a ``send``/``recv``/``drain``
contract: TX via ``send_until_ack``/``send_no_wait``; RX via an ``asyncio.Queue`` fed by a registered
callback that keeps only AP→us frames (Addr1==our MAC, Addr2==BSSID), so a caller
never trips over its own echoed TX or unrelated traffic.

``Association`` does Open-System auth + an Association Request. The assoc-req carries
whatever trailing IE the caller supplies (``assoc_trailer_ies``): a WPS vendor IE, a
forged RSN IE for PMKID, or nothing, so this module knows no protocol above the
802.11 skeleton.
"""

from __future__ import annotations

import asyncio
import logging
from enum import IntEnum
import time
from typing import Callable, Optional

from wifit3.dot11 import build_deauth, random_client_mac, str_to_mac
from wifit3.dot11.auth_assoc import auth_req, assoc_req, status_description
from wifit3.dot11.deauth import reason_description
from wifit3.dot11.packet import AuthPacket, AssocRespPacket, DeauthPacket

logger = logging.getLogger(__name__)


class AssocState(IntEnum):
    """Where a station stands with one AP (802.11-2020 11.3). AUTHENTICATED means
    the 802.11 open-system exchange succeeded, not 802.1X authorization."""

    UNAUTHENTICATED = 1
    AUTHENTICATED = 2
    ASSOCIATED = 3


def build_client_leaving(bssid: bytes, our_mac: bytes, deauth: bool = True) -> bytes:
    """A client→AP deauth (default) / disassoc announcing we're leaving the BSS."""
    return build_deauth(bssid, our_mac, bssid, 3 if deauth else 8, disassoc=not deauth)


class WlanTransport:
    """Send/recv/drain over a live WlanInterface."""

    RX_QUEUE_SIZE = 64

    def __init__(self, iface, bssid: bytes, our_mac: bytes, tx_observer=None):
        self.iface = iface
        self.bssid = bssid
        self.our_mac = our_mac
        self.tx_observer = tx_observer
        self._q: asyncio.Queue = asyncio.Queue(maxsize=self.RX_QUEUE_SIZE)
        self._loop = asyncio.get_running_loop()
        self._active = False

    def _rx_cb(self, pkt) -> None:
        # Keep only AP→us frames: Addr1 (dest) == our MAC, Addr2 (src) == BSSID.
        frame = pkt.raw
        if len(frame) < 24:
            return
        if frame[4:10] != self.our_mac or frame[10:16] != self.bssid:
            return
        # Thread-safe handoff (driver RX may fire from a background reader).
        self._loop.call_soon_threadsafe(self._enqueue_frame, bytes(frame))

    def _enqueue_frame(self, frame: bytes) -> None:
        if self._q.full():
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self._q.put_nowait(frame)

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        # Forged-MAC registration is the campaign's job (via the array); the transport is radio-only.
        self.iface.register_rx_callback(self._rx_cb)

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)

    async def send_until_ack(self, frame: bytes, max_retries: int = 0) -> bool:
        if self.tx_observer is not None:
            self.tx_observer(frame)
        return await self.iface.send_until_ack(frame, max_retries=max_retries)

    async def send_no_wait(self, frame: bytes) -> bool:
        if self.tx_observer is not None:
            self.tx_observer(frame)
        return await self.iface.send_no_wait(frame)

    async def recv(self, timeout: float) -> Optional[bytes]:
        try:
            return await asyncio.wait_for(self._q.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def drain(self) -> None:
        """Drop any queued frames."""
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except asyncio.QueueEmpty:
                break


class Association:
    """Open-System auth + Association Request against one AP."""

    def __init__(self, iface, bssid: str, ssid: str, channel: int,
                 our_mac: Optional[bytes] = None, assoc_timeout: float = 1.5,
                 auth_timeout: float = 1.0,
                 assoc_trailer_ies: bytes = b"",
                 privacy: Optional[bool] = None,
                 should_stop: Optional[Callable[[], bool]] = None):
        self.iface = iface
        self.bssid = bssid.lower()
        self.bssid_bytes = str_to_mac(self.bssid)
        self.ssid = ssid or ""
        self.channel = channel
        self.our_mac = our_mac or random_client_mac()
        self.assoc_timeout = assoc_timeout
        self.auth_timeout = auth_timeout
        self.assoc_trailer_ies = assoc_trailer_ies
        self.privacy = privacy
        self.should_stop = should_stop or (lambda: False)
        self.associated = False
        self.fail_reason: Optional[str] = None
        self._auth_ok = False
        self._assoc_ok = False
        self.auth_status: Optional[int] = None
        self.assoc_status: Optional[int] = None
        self.state = AssocState.UNAUTHENTICATED
        self._active = False

    # ---- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self.iface.register_rx_callback(self._rx_cb)

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        self.iface.unregister_rx_callback(self._rx_cb)

    async def associate(self, attempts: int = 5) -> bool:
        """Open-auth + assoc. Returns True once the AP accepts us (status 0)."""
        if self.iface.current_channel != self.channel:
            await self.iface.set_channel(self.channel)
        for _ in range(attempts):
            if self.should_stop():
                return False
            self._auth_ok = False
            self._assoc_ok = False
            self.auth_status = None
            self.assoc_status = None
            logger.info("-> Auth Req to %s", self.bssid)
            await self._send_until(auth_req(self.bssid_bytes, self.our_mac),
                                   lambda: self._auth_ok, self.auth_timeout)
            # Send Assoc whether or not the Auth Resp surfaced
            logger.info("-> Assoc Req to %s", self.bssid)
            await self._send_until(assoc_req(self.bssid_bytes, self.our_mac, self.ssid,
                                             self.assoc_trailer_ies,
                                             channel=self.channel, privacy=self.privacy),
                                   lambda: self._assoc_ok, self.assoc_timeout)
            if self._assoc_ok:
                self.associated = True
                return True
        self.associated = False
        if not self.fail_reason:
            self.fail_reason = "no Assoc resp"
        return False

    async def authenticate(self) -> bool:
        """Send an Auth Req and wait. True once the AP answers with status 0."""
        if self.iface.current_channel != self.channel:
            await self.iface.set_channel(self.channel)
        self._auth_ok = False
        self.auth_status = None
        logger.info("-> Auth Req to %s", self.bssid)
        await self._send_until(auth_req(self.bssid_bytes, self.our_mac),
                               lambda: self._auth_ok, self.auth_timeout)
        return self._auth_ok

    async def associate_as(self, ssid: str) -> Optional[int]:
        """Send an Assoc Req claiming ``ssid``, without sending an Auth Req first.
        Returns the Assoc Resp status code, or None when the AP never answers. A refusal
        leaves us AUTHENTICATED, so the caller can claim the next SSID directly."""
        self._assoc_ok = False
        self.assoc_status = None
        self.ssid = ssid
        logger.info("-> Assoc Req to %s as %r", self.bssid, ssid)
        await self._send_until(assoc_req(self.bssid_bytes, self.our_mac, ssid,
                                         self.assoc_trailer_ies,
                                         channel=self.channel, privacy=self.privacy),
                               lambda: self.assoc_status is not None, self.assoc_timeout)
        self.associated = self._assoc_ok
        return self.assoc_status

    async def _send_until(self, frame: bytes, done, timeout: float,
                          resend_after: float = 0.4) -> None:
        """Send ``frame`` immediately, then poll ``done()`` up to ``timeout``, resending
        every ``resend_after`` while still silent (covers a lost TX or a lost AP reply)."""
        deadline = time.time() + timeout
        last_send = 0.0
        while time.time() < deadline and not done() and not self.should_stop():
            if time.time() - last_send >= resend_after:
                await self.iface.send_no_wait(frame)
                last_send = time.time()
            await asyncio.sleep(0.02)

    def _rx_cb(self, pkt) -> None:
        if (not self._active or pkt.raw[4:10] != self.our_mac   # addressed to us
                or pkt.raw[10:16] != self.bssid_bytes):         # by the AP we targeted
            return
        if isinstance(pkt, AssocRespPacket):
            self.assoc_status = pkt.status
            desc = status_description(pkt.status)
            if pkt.status == 0:
                self._assoc_ok = True
                self.state = AssocState.ASSOCIATED
                logger.info("<- Assoc Resp (status 0: %s) from %s", desc, self.bssid)
            elif pkt.status is not None:
                self.fail_reason = f"Assoc rejected (status {pkt.status}: {desc})"
                logger.info("<- Assoc Resp rejected (status %s: %s) from %s", pkt.status, desc, self.bssid)
        elif isinstance(pkt, AuthPacket):
            self.auth_status = pkt.status
            desc = status_description(pkt.status)
            if pkt.status == 0:
                self._auth_ok = True
                self.state = AssocState.AUTHENTICATED
                logger.info("<- Auth Resp (status 0: %s) from %s", desc, self.bssid)
            elif pkt.status is not None:
                self.fail_reason = f"Auth rejected (status {pkt.status}: {desc})"
                logger.info("<- Auth Resp rejected (status %s: %s) from %s", pkt.status, desc, self.bssid)
        elif isinstance(pkt, DeauthPacket):
            kind = "disassoc" if getattr(pkt, "type", "") == "mgmt_10" else "deauth"
            desc = reason_description(pkt.reason)
            self.fail_reason = f"{kind} (reason {pkt.reason}: {desc})"
            logger.info("<- %s (reason %s: %s) from %s", kind.upper(), pkt.reason, desc, self.bssid)
            self.associated = False
            self._assoc_ok = False
            self.state = (AssocState.AUTHENTICATED if kind == "disassoc"
                          else AssocState.UNAUTHENTICATED)
