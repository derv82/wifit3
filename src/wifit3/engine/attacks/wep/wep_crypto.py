"""Pure WEP crypto for fragmentation / chopchop (M5/M6).

EVERYTHING HERE IS OFFLINE-TESTABLE — no hardware. Build + unit-test this
module FIRST (like rc4_keystream / PtwCracker were), because it's the trickiest
correctness in M5/M6. The hardware-in-the-loop part (the oracle: recognizing
the AP's relayed frame) lives in fragmentation.py / chopchop.py.

WEP per-frame layout of the encrypted body (after the 24/26-byte MAC header):
    IV(3) | KeyID(1) | RC4( plaintext ++ ICV )      where ICV = CRC32(plaintext)
The MAC header is cleartext; only plaintext++ICV is RC4'd. Encrypting/forging
needs only the *keystream* (IV's RC4 output), never the key — which is the
whole point of frag/chopchop.

Suggested tests (all deterministic):
  - icv() matches a known CRC32 vector.
  - wep_encrypt(ks, pt) then XOR ks back == pt ++ icv(pt).
  - forge_arp_request(ks, ...) decrypts (XOR ks) to a well-formed ARP whose
    trailing 4 bytes are a valid ICV over the rest.
  - chop_last_byte_and_fixup: take ciphertext+ICV with a known plaintext, chop
    + fix with the CORRECT last-byte guess → the shortened frame's ICV is valid
    (decrypts clean); a WRONG guess → invalid ICV.
"""

from __future__ import annotations

# WEP uses CRC-32 (IEEE 802.3) over the plaintext as its integrity check (ICV),
# appended little-endian before encryption.


def icv(plaintext: bytes) -> bytes:
    """4-byte WEP ICV (CRC32 of plaintext, little-endian)."""
    raise NotImplementedError("M5/M6: zlib.crc32(plaintext) → 4 bytes LE")


def wep_encrypt(keystream: bytes, plaintext: bytes) -> bytes:
    """Encrypt under a known keystream: (plaintext ++ icv) XOR keystream.
    Needs len(keystream) >= len(plaintext) + 4. Returns the ciphertext body
    (what follows IV+KeyID on the wire)."""
    raise NotImplementedError("M5/M6")


def forge_arp_request(keystream: bytes, *, sender_mac: bytes, sender_ip: bytes,
                      target_ip: bytes) -> bytes:
    """Forge a broadcast ARP-request *encrypted body* from recovered keystream.
    Plaintext = LLC/SNAP (AA AA 03 00 00 00 08 06) ++ ARP request. The caller
    (campaign) wraps this in a ToDS MAC header from our associated MAC and
    hands it to WepArpReplay. Needs ~ (8 + 28 + 4) bytes of keystream."""
    raise NotImplementedError("M5/M6")


def chop_last_byte_and_fixup(body: bytes, plaintext_guess: int) -> bytes:
    """ChopChop core: given an encrypted body whose ICV is currently valid,
    return a one-byte-shorter body whose ICV is ALSO valid IFF
    ``plaintext_guess`` equals the true last plaintext byte.

    Relies on CRC32 being linear/affine: removing a byte and correcting the
    trailing ICV is a fixed XOR derived from the guessed byte — no key needed.
    This is the math the chopchop oracle queries against the AP 256×/byte.
    """
    raise NotImplementedError("M5/M6 — the KoreK ICV-fixup; unit-test heavily")
