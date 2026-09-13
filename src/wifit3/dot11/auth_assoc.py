"""802.11 Open-System Authentication + Association Request builders (pure spec).

The frame bytes only; the stateful auth+assoc exchange (retries, RX matching) lives in
the campaign that drives these, ``campaigns.auth_assoc``.
"""
import struct
from typing import Optional

from wifit3.dot11.ie import ssid_ie, rates_ie, ext_rates_ie, ht_cap_ie
from wifit3.dot11.mac import mac_header

# Standard 802.11 Authentication / Association response status codes.
STATUS_CODES = {
    0: "success",
    1: "unspecified-failure",
    10: "caps-unsupported",
    12: "assoc-denied-outside-standard",
    13: "rates-unsupported",
    14: "short-slot-unsupported",
    15: "dsss-ofdm-unsupported",
    17: "assoc-denied-ap-busy",
    18: "basic-rates-unsupported",
    19: "short-preamble-unsupported",
    27: "ht-caps-required",
    30: "pmf-required",
    34: "ht-caps-unsupported",
    40: "invalid-ie",
    41: "group-cipher-invalid",
    42: "pairwise-cipher-invalid",
    43: "akm-invalid",
    44: "rsne-version-unsupported",
    45: "rsne-caps-invalid",
    46: "cipher-suite-rejected",
    72: "vht-caps-unsupported",
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
              ht_capable: bool = True) -> bytes:
    """Association Request: band-aware rates, 20MHz HT caps, ESS+(auto)Privacy."""
    if privacy is None:
        has_rsn = b"\x30" in trailer_ies or b"\x00\x50\xf2\x01" in trailer_ies
        has_wps = b"\x00\x50\xf2\x04" in trailer_ies
        privacy = not (has_wps and not has_rsn)

    cap_val = 0x0011 if privacy else 0x0001
    cap = struct.pack("<H", cap_val)
    listen = struct.pack("<H", 0x0001)
    ht = ht_cap_ie() if ht_capable else b""
    ies = ssid_ie(ssid) + rates_ie(channel) + ext_rates_ie(channel) + ht + trailer_ies
    return _hdr(b"\x00\x00", bssid, our_mac) + cap + listen + ies
