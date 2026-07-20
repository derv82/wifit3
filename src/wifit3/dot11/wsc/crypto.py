"""WSC (Wi-Fi Simple Config) crypto core — pure Python, no external deps.

The whole cryptographic surface of the WPS registration protocol, shared by the
online PIN brute-force (registrar building M2/M4/M6) and the future PixieWPS
offline attack. Ported from hostapd's ``src/wps/wps_common.c`` /
``wps_attr_build.c`` (the reference the kernel + reaver/bully all embed).

Anchors (so this isn't validated only against itself):
  * AES-128 — FIPS-197 known-answer vector (``tests``).
  * HMAC-SHA256 / SHA-256 — stdlib ``hmac`` / ``hashlib``.
  * DH MODP group 5 — the RFC 3526 1536-bit constant.
  * The full DH→KDF→AuthKey chain — proven on-air the moment a real AP accepts
    our M2 Authenticator and proceeds to M3 (``scripts/wps/wps_probe.py``).

Key derivation (hostapd ``wps_derive_keys``):
    DHKey = SHA-256( zeropad(g^AB mod p, 192) )
    KDK   = HMAC-SHA256_DHKey( N1 || EnrolleeMAC || N2 )
    KDF(KDK,"Wi-Fi Easy and Secure Key Derivation",640b)
          -> AuthKey(32) || KeyWrapKey(16) || EMSK(32)

The split-PIN derivation (hostapd ``wps_derive_psk`` + ``wps_build_r_hash``):
    PSK1 = first16( HMAC-SHA256_AuthKey(PIN[:4 ASCII]) )
    PSK2 = first16( HMAC-SHA256_AuthKey(PIN[4:8 ASCII]) )
    R-Hash1 = HMAC-SHA256_AuthKey( R-S1 || PSK1 || PKe || PKr )   (R-S2/PSK2 for 2)
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Tuple

# ---------------------------------------------------------------------------
# Lengths (hostapd wps.h)
# ---------------------------------------------------------------------------
NONCE_LEN = 16          # WPS_NONCE_LEN — Enrollee/Registrar nonces
SECRET_NONCE_LEN = 16   # WPS_SECRET_NONCE_LEN — E-S1/E-S2/R-S1/R-S2
PSK_LEN = 16            # WPS_PSK_LEN — half of a SHA-256 digest
HASH_LEN = 32           # E-Hash/R-Hash (full SHA-256)
AUTHKEY_LEN = 32
KEYWRAPKEY_LEN = 16
EMSK_LEN = 32
AUTHENTICATOR_LEN = 8   # WPS_AUTHENTICATOR_LEN — truncated HMAC
KWA_LEN = 8             # WPS_KWA_LEN — Key Wrap Authenticator
PUBKEY_LEN = 192        # DH MODP-1536 public key, zero-padded big-endian

# Diffie-Hellman MODP group 5 (RFC 3526, 1536-bit). Generator = 2.
DH_GENERATOR = 2
DH_PRIME = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1"
    "29024E088A67CC74020BBEA63B139B22514A08798E3404DD"
    "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245"
    "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D"
    "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F"
    "83655D23DCA3AD961C62F356208552BB9ED529077096966D"
    "670C354E4ABC9804F1746C08CA237327FFFFFFFFFFFFFFFF",
    16,
)

_KDF_LABEL = b"Wi-Fi Easy and Secure Key Derivation"


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------
def sha256(*chunks: bytes) -> bytes:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c)
    return h.digest()


def hmac_sha256(key: bytes, *chunks: bytes) -> bytes:
    m = hmac.new(key, digestmod=hashlib.sha256)
    for c in chunks:
        m.update(c)
    return m.digest()


# ---------------------------------------------------------------------------
# Diffie-Hellman (MODP group 5)
# ---------------------------------------------------------------------------
def _int_to_bytes(n: int, length: int) -> bytes:
    return n.to_bytes(length, "big")


def dh_generate_keypair() -> Tuple[int, bytes]:
    """Return (private_int, public_192B). Private key is a 1536-bit random."""
    priv = int.from_bytes(os.urandom(192), "big") % (DH_PRIME - 2) + 2
    pub = pow(DH_GENERATOR, priv, DH_PRIME)
    return priv, _int_to_bytes(pub, PUBKEY_LEN)


def dh_shared_secret(peer_pubkey: bytes, our_priv: int) -> bytes:
    """g^AB mod p, zero-padded big-endian to 192 bytes (hostapd wpabuf_zeropad)."""
    peer = int.from_bytes(peer_pubkey, "big")
    shared = pow(peer, our_priv, DH_PRIME)
    return _int_to_bytes(shared, PUBKEY_LEN)


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------
def wps_kdf(key: bytes, out_len: int) -> bytes:
    """hostapd ``wps_kdf`` with the fixed WSC label, empty prefix.

    res = concat_i( HMAC-SHA256_key( BE32(i) || label || BE32(out_len*8) ) ),
    i = 1..ceil(out_len/32), truncated to out_len.
    """
    key_bits = (out_len * 8).to_bytes(4, "big")
    out = b""
    i = 1
    while len(out) < out_len:
        out += hmac_sha256(key, i.to_bytes(4, "big"), _KDF_LABEL, key_bits)
        i += 1
    return out[:out_len]


def derive_keys(
    dh_shared: bytes, nonce_e: bytes, enrollee_mac: bytes, nonce_r: bytes
) -> Tuple[bytes, bytes, bytes]:
    """Return (authkey, keywrapkey, emsk)."""
    dhkey = sha256(dh_shared)
    kdk = hmac_sha256(dhkey, nonce_e, enrollee_mac, nonce_r)
    keys = wps_kdf(kdk, AUTHKEY_LEN + KEYWRAPKEY_LEN + EMSK_LEN)
    return (
        keys[:AUTHKEY_LEN],
        keys[AUTHKEY_LEN : AUTHKEY_LEN + KEYWRAPKEY_LEN],
        keys[AUTHKEY_LEN + KEYWRAPKEY_LEN :],
    )


def derive_psk(authkey: bytes, pin: str) -> Tuple[bytes, bytes]:
    """PSK1/PSK2 from the device password (PIN), hostapd ``wps_derive_psk``.

    The PIN is hashed as its ASCII decimal string, split into halves of
    ``(len+1)//2`` and ``len//2`` bytes — for an 8-digit PIN that's the first
    and last 4 digits.
    """
    pw = pin.encode("ascii")
    half = (len(pw) + 1) // 2
    psk1 = hmac_sha256(authkey, pw[:half])[:PSK_LEN]
    psk2 = hmac_sha256(authkey, pw[half:])[:PSK_LEN]
    return psk1, psk2


def e_or_r_hash(
    authkey: bytes, secret_nonce: bytes, psk: bytes, pke: bytes, pkr: bytes
) -> bytes:
    """E-Hash{1,2} / R-Hash{1,2} = HMAC_AuthKey(S || PSK || PK_E || PK_R)."""
    return hmac_sha256(authkey, secret_nonce, psk, pke, pkr)


def check_pin_half(
    authkey: bytes,
    secret_nonce: bytes,
    expected_hash: bytes,
    pin_half_ascii: bytes,
    pke: bytes,
    pkr: bytes,
) -> bool:
    """PixieWPS verify primitive: does ``pin_half`` reproduce ``expected_hash``
    given the recovered secret nonce E-S{1,2}? (hostapd-equivalent check.)"""
    psk = hmac_sha256(authkey, pin_half_ascii)[:PSK_LEN]
    return hmac_sha256(authkey, secret_nonce, psk, pke, pkr) == expected_hash


# ---------------------------------------------------------------------------
# Authenticator / Key Wrap Authenticator (truncated HMACs)
# ---------------------------------------------------------------------------
def authenticator(authkey: bytes, last_msg: bytes, current_msg: bytes) -> bytes:
    """Authenticator = first 8 bytes of HMAC_AuthKey(M_prev || M_curr*),
    M_curr* being the current message without its Authenticator attribute."""
    return hmac_sha256(authkey, last_msg, current_msg)[:AUTHENTICATOR_LEN]


def key_wrap_authenticator(authkey: bytes, plaintext: bytes) -> bytes:
    """KWA = first 8 bytes of HMAC_AuthKey(plaintext-before-KWA-attr)."""
    return hmac_sha256(authkey, plaintext)[:KWA_LEN]


# ---------------------------------------------------------------------------
# AES-128-CBC — pure Python (FIPS-197). Only used for WSC Encrypted Settings,
# which are a few dozen bytes per PIN attempt, so speed is irrelevant.
# ---------------------------------------------------------------------------
_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16"
)
_INV_SBOX = bytearray(256)
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_INV_SBOX = bytes(_INV_SBOX)

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _mul(a: int, b: int) -> int:
    """GF(2^8) multiply."""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = _xtime(a)
    return p


def _expand_key(key: bytes) -> list:
    """11 round keys (4 words each) for AES-128."""
    words = [list(key[i : i + 4]) for i in range(0, 16, 4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]                       # RotWord
            temp = [_SBOX[b] for b in temp]                  # SubWord
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return [sum(words[r * 4 : r * 4 + 4], []) for r in range(11)]


def _add_round_key(state: list, rk: list) -> None:
    for i in range(16):
        state[i] ^= rk[i]


def _aes128_encrypt_block(key_schedule: list, block: bytes) -> bytes:
    state = list(block)
    _add_round_key(state, key_schedule[0])
    for rnd in range(1, 10):
        state = [_SBOX[b] for b in state]
        state = _shift_rows(state)
        state = _mix_columns(state)
        _add_round_key(state, key_schedule[rnd])
    state = [_SBOX[b] for b in state]
    state = _shift_rows(state)
    _add_round_key(state, key_schedule[10])
    return bytes(state)


def _aes128_decrypt_block(key_schedule: list, block: bytes) -> bytes:
    state = list(block)
    _add_round_key(state, key_schedule[10])
    for rnd in range(9, 0, -1):
        state = _inv_shift_rows(state)
        state = [_INV_SBOX[b] for b in state]
        _add_round_key(state, key_schedule[rnd])
        state = _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    state = [_INV_SBOX[b] for b in state]
    _add_round_key(state, key_schedule[0])
    return bytes(state)


# State is column-major (AES standard): byte index = col*4 + row.
def _shift_rows(s: list) -> list:
    o = list(s)
    for row in range(1, 4):
        for col in range(4):
            o[col * 4 + row] = s[((col + row) % 4) * 4 + row]
    return o


def _inv_shift_rows(s: list) -> list:
    o = list(s)
    for row in range(1, 4):
        for col in range(4):
            o[col * 4 + row] = s[((col - row) % 4) * 4 + row]
    return o


def _mix_columns(s: list) -> list:
    o = [0] * 16
    for c in range(4):
        col = s[c * 4 : c * 4 + 4]
        o[c * 4 + 0] = _mul(col[0], 2) ^ _mul(col[1], 3) ^ col[2] ^ col[3]
        o[c * 4 + 1] = col[0] ^ _mul(col[1], 2) ^ _mul(col[2], 3) ^ col[3]
        o[c * 4 + 2] = col[0] ^ col[1] ^ _mul(col[2], 2) ^ _mul(col[3], 3)
        o[c * 4 + 3] = _mul(col[0], 3) ^ col[1] ^ col[2] ^ _mul(col[3], 2)
    return o


def _inv_mix_columns(s: list) -> list:
    o = [0] * 16
    for c in range(4):
        col = s[c * 4 : c * 4 + 4]
        o[c * 4 + 0] = _mul(col[0], 14) ^ _mul(col[1], 11) ^ _mul(col[2], 13) ^ _mul(col[3], 9)
        o[c * 4 + 1] = _mul(col[0], 9) ^ _mul(col[1], 14) ^ _mul(col[2], 11) ^ _mul(col[3], 13)
        o[c * 4 + 2] = _mul(col[0], 13) ^ _mul(col[1], 9) ^ _mul(col[2], 14) ^ _mul(col[3], 11)
        o[c * 4 + 3] = _mul(col[0], 11) ^ _mul(col[1], 13) ^ _mul(col[2], 9) ^ _mul(col[3], 14)
    return o


def aes128_cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("AES-CBC plaintext must be a multiple of 16 bytes")
    ks = _expand_key(key)
    prev = iv
    out = bytearray()
    for off in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[off : off + 16], prev))
        prev = _aes128_encrypt_block(ks, block)
        out += prev
    return bytes(out)


def aes128_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("AES-CBC ciphertext must be a multiple of 16 bytes")
    ks = _expand_key(key)
    prev = iv
    out = bytearray()
    for off in range(0, len(data), 16):
        ct = data[off : off + 16]
        dec = _aes128_decrypt_block(ks, ct)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = ct
    return bytes(out)


def pkcs5_pad(data: bytes, block: int = 16) -> bytes:
    pad = block - (len(data) % block)
    return data + bytes([pad]) * pad


def pkcs5_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 16 or pad > len(data):
        return data            # tolerate non-conforming padding rather than raise
    return data[:-pad]


# ---------------------------------------------------------------------------
# WPS PIN checksum (hostapd ``wps_pin_checksum``)
# ---------------------------------------------------------------------------
def pin_checksum(pin_7digit: int) -> int:
    """The 8th digit of a WPS PIN, computed from the first 7."""
    accum = 0
    t = pin_7digit
    while t:
        accum += 3 * (t % 10)
        t //= 10
        accum += t % 10
        t //= 10
    return (10 - accum % 10) % 10


def pin_is_valid(pin: str) -> bool:
    """True iff an 8-digit PIN's last digit is a correct checksum."""
    if len(pin) != 8 or not pin.isdigit():
        return False
    return pin_checksum(int(pin[:7])) == int(pin[7])
