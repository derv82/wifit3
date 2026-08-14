"""WPA/WPA2-PSK key derivation + the 4-way EAPOL MIC (no I/O).

PMK = PBKDF2-HMAC-SHA1(psk, ssid, 4096, 32). PTK = PRF-512 over the sorted MAC pair and
sorted nonce pair. The MIC (key descriptor version 2, plain PSK) is HMAC-SHA1-128 over the
EAPOL payload with its MIC field zeroed, keyed by the KCK (PTK[:16]). Version 3 (PSK-SHA256,
AES-CMAC) is not implemented: the FakeAP twin advertises plain PSK.
"""
import hashlib
import hmac

_KCK_LEN = 16


def pmk(psk: str, ssid: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", psk.encode(), ssid.encode(), 4096, 32)


def _prf(key: bytes, label: bytes, data: bytes, nbytes: int) -> bytes:
    out = b""
    i = 0
    while len(out) < nbytes:
        out += hmac.new(key, label + b"\x00" + data + bytes([i]), hashlib.sha1).digest()
        i += 1
    return out[:nbytes]


def ptk(pmk_bytes: bytes, aa: bytes, spa: bytes, anonce: bytes, snonce: bytes,
        nbytes: int = 48) -> bytes:
    """AA = AP MAC, SPA = client MAC. Both the MAC pair and the nonce pair enter sorted, so
    the two peers derive the same PTK regardless of who is addressed first."""
    data = min(aa, spa) + max(aa, spa) + min(anonce, snonce) + max(anonce, snonce)
    return _prf(pmk_bytes, b"Pairwise key expansion", data, nbytes)


def kck(ptk_bytes: bytes) -> bytes:
    return ptk_bytes[:_KCK_LEN]


def eapol_mic(kck_bytes: bytes, eapol_payload_mic_zeroed: bytes) -> bytes:
    return hmac.new(kck_bytes, eapol_payload_mic_zeroed, hashlib.sha1).digest()[:16]


def mic_for(psk: str, ssid: str, aa: bytes, spa: bytes, anonce: bytes, snonce: bytes,
            eapol_payload_mic_zeroed: bytes) -> bytes:
    """The M2 MIC a client with this PSK would produce over these EAPOL bytes."""
    k = kck(ptk(pmk(psk, ssid), aa, spa, anonce, snonce))
    return eapol_mic(k, eapol_payload_mic_zeroed)
