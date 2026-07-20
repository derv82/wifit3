"""802.11 open-auth + association against one AP, plus a live-interface transport.

``WlanTransport`` adapts a ``WlanInterface`` to a ``send``/``recv``/``drain``
contract: TX via ``send_until_ack``/``send_no_wait``; RX via an ``asyncio.Queue`` fed by a registered
callback that keeps only AP→us frames (Addr1==our MAC, Addr2==BSSID) — so a caller
never trips over its own echoed TX or unrelated traffic.

``Association`` does Open-System auth + an Association Request. The assoc-req carries
whatever trailing IE the caller supplies (``assoc_trailer_ies``) — a WPS vendor IE, a
forged RSN IE for PMKID, or nothing — so this module knows no protocol above the
802.11 skeleton.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable, Optional

from wifit3.dot11 import build_deauth
from wifit3.dot11.auth_assoc import auth_req, assoc_req
from wifit3.dot11.packet import AuthPacket, AssocRespPacket, DeauthPacket

logger = logging.getLogger(__name__)

_SUBTYPE_ASSOC_RESP = 0x01
_SUBTYPE_AUTH = 0x0B
_SUBTYPE_DEAUTH = 0x0C
_SUBTYPE_DISASSOC = 0x0A


def str_to_mac(s: str) -> bytes:
    return bytes(int(x, 16) for x in s.split(":"))


def random_client_mac() -> bytes:
    """Locally-administered unicast MAC."""
    return bytes([0x02]) + os.urandom(5)


def build_client_leaving(bssid: bytes, our_mac: bytes, deauth: bool = True) -> bytes:
    """A client→AP deauth (default) / disassoc announcing we're leaving the BSS.

    Sent when abandoning a stalled WPS attempt so the AP drops our (possibly
    mid-exchange) EAP session. Otherwise it keeps retransmitting the in-flight WSC
    message to our now-dead MAC and won't start a fresh session for the next
    attempt's new MAC — the "stuck at Identity" lockout. Reason 3 = STA leaving
    (deauth); reason 8 = STA leaving (disassoc). addr1=AP, addr2=us, addr3=AP."""
    return build_deauth(bssid, our_mac, bssid, 3 if deauth else 8, disassoc=not deauth)


class WlanTransport:
    """Send/recv/drain over a live WlanInterface."""

    def __init__(self, iface, bssid: bytes, our_mac: bytes, tx_observer=None):
        self.iface = iface
        self.bssid = bssid
        self.our_mac = our_mac
        # Optional callback(frame_bytes) invoked on every TX — lets a probe
        # record our injected frames alongside RX for a full-conversation pcap.
        self.tx_observer = tx_observer
        self._q: asyncio.Queue = asyncio.Queue()
        self._loop = asyncio.get_event_loop()
        self._active = False

    def _rx_cb(self, pkt) -> None:
        # Keep only AP→us frames: Addr1 (dest) == our MAC, Addr2 (src) == BSSID.
        frame = pkt.raw
        if len(frame) < 24:
            return
        if frame[4:10] != self.our_mac or frame[10:16] != self.bssid:
            return
        # Thread-safe handoff (driver RX may fire from a background reader).
        self._loop.call_soon_threadsafe(self._q.put_nowait, bytes(frame))

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self.iface.register_forged_mac(self.our_mac)
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
        """Drop any queued frames — called between PIN attempts on a kept-alive
        association so a previous attempt's late retransmits don't bleed in."""
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
                 should_stop: Optional[Callable[[], bool]] = None):
        self.iface = iface
        self.bssid = bssid.lower()
        self.bssid_bytes = str_to_mac(self.bssid)
        self.ssid = ssid or ""
        self.channel = channel
        self.our_mac = our_mac or random_client_mac()
        self.assoc_timeout = assoc_timeout
        self.auth_timeout = auth_timeout
        # A complete trailing IE (tag+len+body) appended to the Assoc Request — e.g. a
        # WPS vendor IE (registrar/enrollee intent) or PMKID's forged single-AKM RSN IE.
        # Empty = a bare Assoc Request (SSID + rates only).
        self.assoc_trailer_ies = assoc_trailer_ies
        # Polled in associate()/_send_until so a user Stop aborts the resend loop
        # promptly instead of injecting for the full auth+assoc budget.
        self.should_stop = should_stop or (lambda: False)
        self.associated = False
        self.fail_reason: Optional[str] = None
        self._auth_ok = False
        self._assoc_ok = False
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
        """Open-auth + assoc. Returns True once the AP accepts us (status 0).

        Waits for the Open-System Auth Resp (status 0) before sending the Assoc
        Req: an AP drops an Assoc from a not-yet-authenticated STA, so a blind
        delay races a slow/cold AP and whiffs first contact. Falls back to sending
        the Assoc anyway after ``auth_timeout`` for APs/captures that don't surface
        a matchable Auth Resp.

        Our auth/assoc frames land no-ACK/no-retry, so within each wait we *resend*
        while silent (a dropped auth-req or assoc-req is otherwise a lost attempt).
        Hardware ground truth (AirLink): association was the top failure at ~29% until
        this + relying on the AP's own retransmits (no active-monitor)."""
        if self.iface.current_channel != self.channel:
            await self.iface.set_channel(self.channel)
        for _ in range(attempts):
            if self.should_stop():
                return False
            self._auth_ok = False
            self._assoc_ok = False
            await self._send_until(auth_req(self.bssid_bytes, self.our_mac),
                                   lambda: self._auth_ok, self.auth_timeout)
            # Send Assoc whether or not the Auth Resp surfaced (fallback for APs that
            # don't emit a matchable one) — resend while waiting for the Assoc Resp.
            await self._send_until(assoc_req(self.bssid_bytes, self.our_mac, self.ssid,
                                             self.assoc_trailer_ies),
                                   lambda: self._assoc_ok, self.assoc_timeout)
            if self._assoc_ok:
                self.associated = True
                return True
        self.associated = False
        if not self.fail_reason:
            self.fail_reason = "no Assoc resp"
        return False

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
        if not self._active or pkt.raw[4:10] != self.our_mac:   # addressed to us
            return
        if isinstance(pkt, AssocRespPacket):
            if pkt.status == 0:
                self._assoc_ok = True
            elif pkt.status is not None:
                self.fail_reason = f"Assoc rejected (status {pkt.status})"
        elif isinstance(pkt, AuthPacket):
            if pkt.status == 0:
                self._auth_ok = True
            elif pkt.status is not None:
                self.fail_reason = f"Auth rejected (status {pkt.status})"
        elif isinstance(pkt, DeauthPacket):
            self.associated = False
            self._assoc_ok = False
