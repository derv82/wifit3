"""802.11 Open-System Authentication + Association Request builders (pure spec).

The frame bytes only; the stateful auth+assoc exchange (retries, RX matching) lives in
the campaign that drives these, ``campaigns.auth_assoc``.
"""
import struct

from wifit3.dot11.ie import ssid_ie, rates_ie, ext_rates_ie

# Assoc-request capability: ESS + Privacy. Distinct from the probe-response capability
# (see dot11.probe._CAPABILITY_INFO). Do not conflate.
_CAP_ESS_PRIVACY = 0x0011


def _hdr(fc: bytes, bssid: bytes, our_mac: bytes) -> bytes:
    """24-byte management header for a client->AP frame: addr1 = addr3 = bssid, addr2 =
    our forged STA. Duration and sequence are 0 (the chip fills the sequence)."""
    return fc + b"\x00\x00" + bssid + our_mac + bssid + b"\x00\x00"


def auth_req(bssid: bytes, our_mac: bytes) -> bytes:
    """Open-System Authentication Request (algorithm 0, sequence 1, status 0)."""
    return _hdr(b"\xb0\x00", bssid, our_mac) + b"\x00\x00\x01\x00\x00\x00"


def assoc_req(bssid: bytes, our_mac: bytes, ssid: str, trailer_ies: bytes = b"") -> bytes:
    """Association Request: ESS+Privacy capabilities, listen interval, SSID + rates, then
    any ``trailer_ies`` the caller appends (a forced-PSK RSN IE for PMKID, a WPS vendor IE
    for the WPS exchange, or nothing for plain/WEP association)."""
    cap = struct.pack("<H", _CAP_ESS_PRIVACY)
    listen = struct.pack("<H", 0x0001)
    ies = ssid_ie(ssid) + rates_ie() + ext_rates_ie() + trailer_ies
    return _hdr(b"\x00\x00", bssid, our_mac) + cap + listen + ies
