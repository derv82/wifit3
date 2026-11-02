"""One beacon per WPA3-Enterprise RSN profile, for tests/wlan/test_wpa3_enterprise_pcap.py.

Run from the repo root to write them to a pcap for inspection:
    PYTHONPATH=src python tests/fixtures/make_wpa3_enterprise_pcap.py
"""
from __future__ import annotations

from pathlib import Path

from wifit3.persist.pcap import write_pcap

OUI = b"\x00\x0f\xac"
BROADCAST = b"\xff" * 6
CHANNEL = 1

# name, bssid tail, group cipher, pairwise ciphers, AKMs, RSN caps, group mgmt cipher (GMCS)
PROFILES = [
    ("wpa2-enterprise-control",             0x01, 4, (4,),   (1,),   0x00C0, 6),
    ("wpa3-enterprise",                     0x02, 4, (4,),   (5,),   0x00C0, 6),
    ("wpa3-enterprise-transition",          0x03, 4, (4,),   (1, 5), 0x0080, 6),
    ("wpa3-enterprise-192",                 0x04, 9, (9,),   (12,),  0x00C0, 12),
    ("sha256-without-required-pmf-control", 0x05, 4, (4,),   (5,),   0x0080, 6),
    ("suite-b-192-missing-gmcs-control",    0x06, 9, (9,),   (12,),  0x00C0, None),
    ("suite-b-192-wrong-group-control",     0x07, 4, (9,),   (12,),  0x00C0, 12),
]


def _suite(number: int) -> bytes:
    return OUI + bytes([number])


def _rsn_ie(group: int, pairwise, akms, caps: int, mgmt) -> bytes:
    body = b"\x01\x00" + _suite(group)
    body += len(pairwise).to_bytes(2, "little") + b"".join(_suite(c) for c in pairwise)
    body += len(akms).to_bytes(2, "little") + b"".join(_suite(a) for a in akms)
    body += caps.to_bytes(2, "little")
    if mgmt is not None:
        body += b"\x00\x00" + _suite(mgmt)
    return bytes([48, len(body)]) + body


def _beacon(bssid: bytes, ssid: bytes, rsn: bytes) -> bytes:
    header = b"\x80\x00\x00\x00" + BROADCAST + bssid + bssid + b"\x00\x00"
    fixed = b"\x00" * 8 + b"\x64\x00" + b"\x31\x04"   # timestamp + beacon interval + caps
    ies = (bytes([0, len(ssid)]) + ssid + b"\x01\x04\x82\x84\x8b\x96"
           + bytes([3, 1, CHANNEL]) + rsn)
    return header + fixed + ies


def beacons() -> list[tuple[bytes, float]]:
    """(frame, timestamp) for each profile in PROFILES, in order."""
    records = []
    for i, (name, tail, group, pairwise, akms, caps, mgmt) in enumerate(PROFILES):
        bssid = bytes.fromhex("0273a30000") + bytes([tail])
        rsn = _rsn_ie(group, pairwise, akms, caps, mgmt)
        records.append((_beacon(bssid, name[:32].encode("ascii"), rsn), 1_700_000_000.0 + i))
    return records


def main() -> None:
    out = Path(__file__).with_name("wpa3_enterprise.pcap")
    print(f"wrote {write_pcap(out, beacons())} beacons -> {out}")


if __name__ == "__main__":
    main()
