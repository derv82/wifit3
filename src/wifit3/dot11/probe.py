"""802.11 Probe Request + Probe Response builders (pure spec)."""
import struct
import time

from wifit3.dot11.ie import ssid_ie, rates_ie, ext_rates_ie, ds_param_ie, GENERIC_RSN_IE

# Probe-response capability: ESS + Privacy + Short Slot Time. Distinct from the
# auth/assoc ESS+Privacy 0x0011 (see dot11.auth_assoc) — do not conflate.
_CAPABILITY_INFO = 0x0411


def probe_req(bssid: bytes, our_mac: bytes, ssid: str) -> bytes:
    """Directed Probe Request for ``ssid``, addressed to ``bssid`` (RA/BSSID), from our
    forged STA. The AP answers only if the SSID matches (or it responds broadly)."""
    hdr = b"\x40\x00" + b"\x00\x00" + bssid + our_mac + bssid + b"\x00\x00"
    return hdr + ssid_ie(ssid) + rates_ie() + ext_rates_ie()


def probe_resp(bssid: bytes, ssid: str, channel: int) -> bytes:
    """Forged WPA2-only Probe Response with Addr1 zeroed. The caller splices the requesting
    client's MAC into bytes [4:10] before injecting, so build once and re-splice per probe.
    The TSF timestamp is stamped at build time."""
    hdr = b"\x50\x00" + b"\x00\x00" + b"\x00" * 6 + bssid + bssid + b"\x00\x00"
    fixed = (struct.pack("<Q", int(time.time() * 1_000_000))
             + struct.pack("<H", 100)                      # beacon interval, 100 TU
             + struct.pack("<H", _CAPABILITY_INFO))
    tags = (ssid_ie(ssid) + rates_ie() + ds_param_ie(channel)
            + ext_rates_ie() + GENERIC_RSN_IE)
    return hdr + fixed + tags
