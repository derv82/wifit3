from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass

from wifit3.campaigns.auth_assoc import Association, WlanTransport, build_client_leaving
from wifit3.dot11 import str_to_mac
from wifit3.wlan.fingerprinting.router import RouterClaim, RouterEvidence
from wifit3.wlan.lease import SPOOFABLE

_LLC_SNAP_IPV4 = b"\xaa\xaa\x03\x00\x00\x00\x08\x00"
_BROADCAST = b"\xff" * 6
_MIKROTIK_PORTS = {5678, 20561}
_MNDP_DISCOVERY = b"\x00\x00\x00\x00"
_WINBOX_DISCOVERY = b"M2"


@dataclass(frozen=True)
class MikrotikProbeResult:
    ok: bool
    source: str = ""
    claims: tuple[RouterClaim, ...] = ()
    detail: str = ""


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


def build_mikrotik_discovery_frames(bssid: bytes, our_mac: bytes) -> tuple[bytes, ...]:
    header = b"\x08\x01\x00\x00" + bssid + our_mac + _BROADCAST + b"\x00\x00"
    return (
        header + _LLC_SNAP_IPV4 + _ipv4_udp(5678, 5678, _MNDP_DISCOVERY),
        header + _LLC_SNAP_IPV4 + _ipv4_udp(20561, 20561, _WINBOX_DISCOVERY),
    )


def _mikrotik_ports_in_frame(frame: bytes) -> frozenset[int]:
    if len(frame) < 24 + len(_LLC_SNAP_IPV4) + 28:
        return frozenset()
    fc0, fc1 = frame[0], frame[1]
    if ((fc0 >> 2) & 0x03) != 2 or fc1 & 0x40:
        return frozenset()
    body = frame[24:]
    if not body.startswith(_LLC_SNAP_IPV4):
        return frozenset()
    ip = body[len(_LLC_SNAP_IPV4):]
    if len(ip) < 28 or ip[0] >> 4 != 4 or ip[9] != 17:
        return frozenset()
    ihl = (ip[0] & 0x0F) * 4
    if len(ip) < ihl + 8:
        return frozenset()
    src_port, dst_port = struct.unpack(">HH", ip[ihl:ihl + 4])
    return frozenset(port for port in (src_port, dst_port) if port in _MIKROTIK_PORTS)


def is_mikrotik_plaintext_frame(frame: bytes) -> bool:
    return bool(_mikrotik_ports_in_frame(frame))


def is_mikrotik_response(frame: bytes) -> bool:
    if len(frame) < 24:
        return False
    fc1 = frame[1]
    return bool(fc1 & 0x02) and not (fc1 & 0x01) and is_mikrotik_plaintext_frame(frame)


def mikrotik_claims(source: str, *, passive: bool, confidence: float = 0.99) -> tuple[RouterClaim, ...]:
    evidence = RouterEvidence(source, "reachable", "true", confidence, passive=passive)
    return (
        RouterClaim("vendor", "MikroTik", confidence, (evidence,)),
        RouterClaim("kind", "router", confidence, (evidence,)),
    )


def mikrotik_claims_from_frame(frame: bytes, *, passive: bool) -> tuple[RouterClaim, ...]:
    ports = _mikrotik_ports_in_frame(frame)
    if 20561 in ports:
        return mikrotik_claims("mikrotik.mac_winbox", passive=passive, confidence=0.99)
    if 5678 in ports:
        return mikrotik_claims("mikrotik.neighbor", passive=passive, confidence=0.70)
    return ()


async def probe_mikrotik(array, ap, iface=None, timeout: float = 2.0) -> MikrotikProbeResult:
    bssid = ap.bssid.lower()
    bssid_bytes = str_to_mac(bssid)
    try:
        lease = array.lease(channel=ap.channel, fake_mac=SPOOFABLE, bssid=bssid_bytes,
                            ack_tally=True, iface=iface)
    except Exception as exc:
        return MikrotikProbeResult(False, detail=str(exc))

    async with lease as iface:
        if lease.mac is None:
            return MikrotikProbeResult(False, detail="active monitor unavailable")
        our_mac = str_to_mac(lease.mac)
        assoc = Association(iface, bssid, ap.ssid or "", ap.channel, our_mac=our_mac)
        transport = WlanTransport(iface, bssid_bytes, our_mac)
        assoc.start()
        transport.start()
        try:
            if not await assoc.associate(attempts=2):
                return MikrotikProbeResult(False, detail=assoc.fail_reason or "no association response")
            for frame in build_mikrotik_discovery_frames(bssid_bytes, our_mac):
                await iface.send_no_wait(frame)
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                frame = await transport.recv(0.25)
                if frame is not None and is_mikrotik_response(frame):
                    claims = mikrotik_claims_from_frame(frame, passive=False)
                    return MikrotikProbeResult(True, source=claims[0].evidence[0].source, claims=claims)
        finally:
            transport.stop()
            assoc.stop()
            try:
                await iface.send_no_wait(build_client_leaving(bssid_bytes, our_mac))
            except Exception:
                pass
    return MikrotikProbeResult(False, detail="no WinBox response")
