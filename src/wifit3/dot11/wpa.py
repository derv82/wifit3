"""WPA/WPA2-PSK 4-way handshake key derivation and EAPOL-M2 frame building.

Pure 802.11i specification functions: no I/O, no hardware.
"""
from __future__ import annotations

import hashlib
import hmac
import os

from wifit3.dot11.eapol import (
    LLC_SNAP_EAPOL,
    data_header,
    eapol_key,
    set_mic,
)

# Key Info bits for Supplicant Message 2 (M2):
# Bit 0-2: Key Descriptor Version = 2 (HMAC-SHA1 MIC / AES-128-CCMP)
# Bit 3: Key Type = 1 (Pairwise)
# Bit 8: MIC bit set (1)
_M2_KEY_INFO = 0x010A
KCK_LEN = 16


def derive_pmk(passphrase: str, ssid: bytes) -> bytes:
    """802.11i PSK passphrase to PMK: PBKDF2-HMAC-SHA1(passphrase, ssid, 4096, 32)."""
    return hashlib.pbkdf2_hmac("sha1", passphrase.encode(), ssid, 4096, 32)


def prf(key: bytes, label: bytes, data: bytes, nbytes: int) -> bytes:
    """802.11i PRF-n (HMAC-SHA1), truncated to nbytes."""
    out = b""
    i = 0
    while len(out) < nbytes:
        out += hmac.new(key, label + b"\x00" + data + bytes([i]), hashlib.sha1).digest()
        i += 1
    return out[:nbytes]


def derive_ptk(pmk: bytes, ap_mac: bytes, sta_mac: bytes, anonce: bytes, snonce: bytes,
               nbytes: int) -> bytes:
    """First nbytes of the PTK; MAC and nonce pairs enter sorted. KCK is PTK[:KCK_LEN]."""
    mac_block = min(ap_mac, sta_mac) + max(ap_mac, sta_mac)
    nonce_block = min(anonce, snonce) + max(anonce, snonce)
    return prf(pmk, b"Pairwise key expansion", mac_block + nonce_block, nbytes)


def eapol_mic(kck: bytes, eapol_payload_mic_zeroed: bytes) -> bytes:
    """Key descriptor version 2 MIC: HMAC-SHA1-128 over the EAPOL payload with its MIC zeroed."""
    return hmac.new(kck, eapol_payload_mic_zeroed, hashlib.sha1).digest()[:16]


def build_eapol_m2(
    bssid: bytes,
    sta_mac: bytes,
    snonce: bytes,
    kck: bytes,
    replay: int = 1,
    rsn_ie: bytes = b"",
) -> bytes:
    """Construct a full 802.11 Data frame carrying EAPOL-Key Message 2 with MIC."""
    hdr = data_header(to_ds=True, bssid=bssid, client=sta_mac)
    raw_payload = eapol_key(
        key_info=_M2_KEY_INFO,
        key_len=0,
        replay=replay,
        nonce=snonce,
        key_data=rsn_ie,
        mic=bytes(16),
    )
    return hdr + LLC_SNAP_EAPOL + set_mic(raw_payload, eapol_mic(kck, raw_payload))


def random_nonce() -> bytes:
    """Generate a random 32-byte SNonce."""
    return os.urandom(32)
