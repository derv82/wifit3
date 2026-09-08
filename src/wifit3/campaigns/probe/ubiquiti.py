from __future__ import annotations

import asyncio
import struct
from typing import TYPE_CHECKING

from wifit3.campaigns.auth_assoc import Association, WlanTransport, build_client_leaving
from wifit3.campaigns.probe.base import BaseApProbe, ProbeResult
from wifit3.dot11 import str_to_mac
from wifit3.id import RouterClaim, RouterEvidence

if TYPE_CHECKING:
    from wifit3.models import AccessPoint
    from wifit3.wlan.interface import WlanInterface

_LLC_SNAP_IPV4 = b"\xaa\xaa\x03\x00\x00\x00\x08\x00"
_BROADCAST = b"\xff" * 6
_UBNT_PORT = 10001
_UBNT_DISCOVERY = b"\x01\x00\x00\x00"


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f">{len(data) // 2}H", data))
    while total > 0xFFFF:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _ipv4_udp(src_port: int, dst_port: int, payload: bytes) -> bytes:
    udp_len = 8 + len(payload)
    udp = struct.pack(">HHHH", src_port, dst_port, udp_len, 0) + payload
    ip_len = 20 + len(udp)
    ip = bytearray(
        b"\x45\x00" + struct.pack(">H", ip_len) + b"\x00\x00\x00\x00\x40\x11\x00\x00"
        + b"\x00\x00\x00\x00" + b"\xff\xff\xff\xff"
    )
    struct.pack_into(">H", ip, 10, _checksum(bytes(ip)))
    return bytes(ip) + udp


def build_ubnt_discovery_frame(bssid: bytes, our_mac: bytes) -> bytes:
    header = b"\x08\x01\x00\x00" + bssid + our_mac + _BROADCAST + b"\x00\x00"
    return header + _LLC_SNAP_IPV4 + _ipv4_udp(_UBNT_PORT, _UBNT_PORT, _UBNT_DISCOVERY)


def is_ubnt_plaintext_frame(frame: bytes) -> bool:
    if len(frame) < 24 + len(_LLC_SNAP_IPV4) + 28:
        return False
    fc0, fc1 = frame[0], frame[1]
    if ((fc0 >> 2) & 0x03) != 2 or fc1 & 0x40:
        return False
    body = frame[24:]
    if not body.startswith(_LLC_SNAP_IPV4):
        return False
    ip = body[len(_LLC_SNAP_IPV4):]
    if len(ip) < 28 or ip[0] >> 4 != 4 or ip[9] != 17:
        return False
    ihl = (ip[0] & 0x0F) * 4
    if len(ip) < ihl + 8:
        return False
    src_port, dst_port = struct.unpack(">HH", ip[ihl:ihl + 4])
    return src_port == _UBNT_PORT or dst_port == _UBNT_PORT


def is_ubnt_response(frame: bytes) -> bool:
    if len(frame) < 24:
        return False
    fc1 = frame[1]
    return bool(fc1 & 0x02) and not (fc1 & 0x01) and is_ubnt_plaintext_frame(frame)


def ubnt_claims(source: str, *, passive: bool) -> tuple[RouterClaim, ...]:
    evidence = RouterEvidence(source, "reachable", "true", 0.99, passive=passive)
    return (
        RouterClaim("vendor", "Ubiquiti", 0.99, (evidence,)),
        RouterClaim("kind", "router", 0.99, (evidence,)),
    )


class UbiquitiProbe(BaseApProbe):
    """Probes an OPEN AP for Ubiquiti discovery responses on UDP port 10001."""
    name = "ubiquiti"

    def can_probe(self, ap: AccessPoint) -> bool:
        return ap.encryption == "OPEN"

    async def probe(self, iface: WlanInterface, ap: AccessPoint, timeout: float = 2.0) -> ProbeResult:
        bssid_bytes = str_to_mac(ap.bssid.lower())
        await iface.set_channel(ap.channel)
        fake_mac = await iface.set_fake_mac(None, bssid_bytes)
        our_mac_str = fake_mac or (iface.mac_address if isinstance(iface.mac_address, str) else None)
        if our_mac_str is None:
            return ProbeResult(False, detail="active monitor unavailable")
        our_mac = str_to_mac(our_mac_str)

        assoc = Association(iface, ap.bssid.lower(), ap.ssid or "", ap.channel, our_mac=our_mac)
        transport = WlanTransport(iface, bssid_bytes, our_mac)
        assoc.start()
        transport.start()
        try:
            if not await assoc.associate(attempts=2):
                return ProbeResult(False, detail=assoc.fail_reason or "no association response")
            await iface.send_no_wait(build_ubnt_discovery_frame(bssid_bytes, our_mac))
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                frame = await transport.recv(0.25)
                if frame is not None and is_ubnt_response(frame):
                    return ProbeResult(True, source="ubnt.discovery", claims=ubnt_claims("ubnt.discovery", passive=False))
        finally:
            transport.stop()
            assoc.stop()
            try:
                await iface.send_no_wait(build_client_leaving(bssid_bytes, our_mac))
            except Exception:
                pass
            await iface.clear_fake_mac()

        return ProbeResult(False, detail="no UBNT discovery response")
