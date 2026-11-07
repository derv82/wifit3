"""802.11 Open-System Authentication + Association Request builders (pure spec).

The frame bytes only; the stateful auth+assoc exchange (retries, RX matching) lives in
the campaign that drives these, ``wlan.station``.
"""
import struct
from typing import Optional

from wifit3.dot11.ie import ssid_ie, rates_ie, ext_rates_ie, ht_cap_ie
from wifit3.dot11.mac import mac_header

CAP_ESS = 0x0001
CAP_PRIVACY = 0x0010
CAP_SHORT_PREAMBLE = 0x0020
CAP_SHORT_SLOT = 0x0400
_BEACON_CAPABILITY_OFFSET = 34   # 24B MAC header + 8B timestamp + 2B beacon interval

# 802.11 Authentication / Association response status codes (hostapd ieee802_11_defs.h).
STATUS_CODES = {
    0: "success",
    1: "unspecified-failure",
    10: "caps-unsupported",
    12: "assoc-denied-outside-standard",
    13: "auth-algorithm-unsupported",
    14: "unknown-auth-transaction",
    15: "challenge-failure",
    16: "auth-timeout",
    17: "assoc-denied-ap-busy",
    18: "basic-rates-unsupported",
    19: "short-preamble-unsupported",
    25: "short-slot-unsupported",
    27: "ht-caps-required",
    30: "assoc-rejected-temporarily",
    31: "pmf-policy-violation",
    34: "poor-channel-conditions",
    40: "invalid-ie",
    41: "group-cipher-invalid",
    42: "pairwise-cipher-invalid",
    43: "akm-invalid",
    44: "rsne-version-unsupported",
    45: "rsne-caps-invalid",
    46: "cipher-suite-rejected",
    51: "listen-interval-too-large",
    53: "invalid-pmkid",
    54: "invalid-mde",
    72: "invalid-rsne",
    104: "vht-caps-required",
}


def status_description(code: Optional[int]) -> str:
    """Human name for an 802.11 auth/assoc status code."""
    if code is None:
        return "none"
    return STATUS_CODES.get(code, f"unknown(0x{code:02x})")


def _hdr(fc: bytes, bssid: bytes, our_mac: bytes) -> bytes:
    """24-byte management header for a client->AP frame: addr1 = addr3 = bssid, addr2 =
    our forged STA. Duration and sequence are 0 (the chip fills the sequence)."""
    return mac_header(fc, bssid, our_mac, bssid)


def auth_req(bssid: bytes, our_mac: bytes) -> bytes:
    """Open-System Authentication Request (algorithm 0, sequence 1, status 0)."""
    return _hdr(b"\xb0\x00", bssid, our_mac) + b"\x00\x00\x01\x00\x00\x00"


def assoc_req(bssid: bytes, our_mac: bytes, ssid: str, trailer_ies: bytes = b"",
              channel: int = 1, privacy: Optional[bool] = None,
              ht_capable: bool = True, beacon_frame: Optional[bytes] = None) -> bytes:
    """Association Request: band-aware rates, 20MHz HT caps, and the capability field: ESS plus
    the AP's Privacy, Short Preamble and Short Slot bits from ``beacon_frame`` when given."""
    beacon_capability = None
    if beacon_frame is not None and len(beacon_frame) >= _BEACON_CAPABILITY_OFFSET + 2:
        beacon_capability = struct.unpack_from("<H", beacon_frame, _BEACON_CAPABILITY_OFFSET)[0]

    if privacy is None:
        has_rsn = b"\x30" in trailer_ies or b"\x00\x50\xf2\x01" in trailer_ies
        has_wps = b"\x00\x50\xf2\x04" in trailer_ies
        if has_wps and not has_rsn:
            privacy = False
        elif beacon_capability is not None:
            privacy = bool(beacon_capability & CAP_PRIVACY)
        else:
            privacy = True

    cap_val = CAP_ESS
    if privacy:
        cap_val |= CAP_PRIVACY
    if beacon_capability is not None:
        cap_val |= beacon_capability & (CAP_SHORT_PREAMBLE | CAP_SHORT_SLOT)
    elif channel <= 14:
        cap_val |= CAP_SHORT_PREAMBLE
    cap = struct.pack("<H", cap_val)
    listen = struct.pack("<H", 0x0001)
    ht = ht_cap_ie() if ht_capable else b""
    ies = ssid_ie(ssid) + rates_ie(channel) + ext_rates_ie(channel) + ht + trailer_ies
    return _hdr(b"\x00\x00", bssid, our_mac) + cap + listen + ies
