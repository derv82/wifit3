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
import struct
import time
from typing import Callable, Optional

from wifit3.wlan.interface import build_deauth

logger = logging.getLogger(__name__)

# Supported-rates menu (APs only check it parses).
_SUPPORTED_RATES = bytes([0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24])
_EXT_SUPPORTED_RATES = bytes([0x30, 0x48, 0x60, 0x6C])
_CAP_ESS_PRIVACY = 0x0011

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

    def __init__(self, iface, bssid: bytes, our_mac: bytes, tx_observer=None, ack=False):
        self.iface = iface
        self.bssid = bssid
        self.our_mac = our_mac
        # Optional callback(frame_bytes) invoked on every TX — lets a probe
        # record our injected frames alongside RX for a full-conversation pcap.
        self.tx_observer = tx_observer
        # ack=True requires active station mode (see interface.py:set_fake_mac).
        self.ack = ack
        self._q: asyncio.Queue = asyncio.Queue()
        self._loop = asyncio.get_event_loop()
        self._active = False

    def _rx_cb(self, frame: bytes, rssi: int, ts: float) -> None:
        # Keep only AP→us frames: Addr1 (dest) == our MAC, Addr2 (src) == BSSID.
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
        return await self.iface.send_until_ack(frame, max_retries=max_retries,
                                               use_no_ack=not self.ack)

    async def send_no_wait(self, frame: bytes) -> bool:
        if self.tx_observer is not None:
            self.tx_observer(frame)
        return await self.iface.send_no_wait(frame, use_no_ack=not self.ack)

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

    # ---- frame builders ----------------------------------------------------
    def _hdr(self, fc: bytes) -> bytes:
        return (fc + b"\x00\x00" + self.bssid_bytes + self.our_mac
                + self.bssid_bytes + b"\x00\x00")

    def _auth_req(self) -> bytes:
        return self._hdr(b"\xb0\x00") + b"\x00\x00\x01\x00\x00\x00"  # open, seq 1, status 0

    def _assoc_req(self) -> bytes:
        cap = struct.pack("<H", _CAP_ESS_PRIVACY)
        listen = struct.pack("<H", 0x0001)
        ssid = self.ssid.encode("utf-8", "ignore")[:32]
        ies = (
            bytes([0x00, len(ssid)]) + ssid
            + bytes([0x01, len(_SUPPORTED_RATES)]) + _SUPPORTED_RATES
            + bytes([0x32, len(_EXT_SUPPORTED_RATES)]) + _EXT_SUPPORTED_RATES
            + self.assoc_trailer_ies
        )
        return self._hdr(b"\x00\x00") + cap + listen + ies

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
            await self._send_until(self._auth_req(), lambda: self._auth_ok, self.auth_timeout)
            # Send Assoc whether or not the Auth Resp surfaced (fallback for APs that
            # don't emit a matchable one) — resend while waiting for the Assoc Resp.
            await self._send_until(self._assoc_req(), lambda: self._assoc_ok, self.assoc_timeout)
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

    def _rx_cb(self, frame: bytes, rssi: int, ts: float) -> None:
        if not self._active or len(frame) < 24:
            return
        if (frame[0] & 0x0C) != 0x00:          # management only
            return
        if frame[4:10] != self.our_mac:        # addressed to us
            return
        subtype = (frame[0] & 0xF0) >> 4
        if subtype == _SUBTYPE_ASSOC_RESP and len(frame) >= 28:
            status = struct.unpack("<H", frame[26:28])[0]
            if status == 0:
                self._assoc_ok = True
            else:
                self.fail_reason = f"Assoc rejected (status {status})"
        elif subtype == _SUBTYPE_AUTH and len(frame) >= 30:
            status = struct.unpack("<H", frame[28:30])[0]
            if status == 0:
                self._auth_ok = True
            else:
                self.fail_reason = f"Auth rejected (status {status})"
        elif subtype in (_SUBTYPE_DEAUTH, _SUBTYPE_DISASSOC):
            self.associated = False
            self._assoc_ok = False
