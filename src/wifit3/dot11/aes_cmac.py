"""AES-128 encryption (FIPS-197) and AES-CMAC (RFC 4493), the key descriptor version 3 EAPOL MIC.

Table-based (T-table) AES, encrypt direction only.
"""
from __future__ import annotations

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)


def _xtime(b: int) -> int:
    return ((b << 1) ^ (0x1B if b & 0x80 else 0)) & 0xFF


def _ror8(word: int) -> int:
    return ((word >> 8) | (word << 24)) & 0xFFFFFFFF


_T0 = [(_xtime(s) << 24) | (s << 16) | (s << 8) | (_xtime(s) ^ s) for s in _SBOX]
_T1 = [_ror8(t) for t in _T0]
_T2 = [_ror8(t) for t in _T1]
_T3 = [_ror8(t) for t in _T2]
_ROUND_CONSTANTS = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)
_MASK_128 = (1 << 128) - 1
_CMAC_RB = 0x87
_BLOCK_LEN = 16


def _expand_key(key: bytes) -> list[int]:
    w = [int.from_bytes(key[i:i + 4], "big") for i in range(0, 16, 4)]
    for rcon in _ROUND_CONSTANTS:
        t = w[-1]
        t = ((_SBOX[(t >> 16) & 0xFF] << 24) | (_SBOX[(t >> 8) & 0xFF] << 16)
             | (_SBOX[t & 0xFF] << 8) | _SBOX[t >> 24]) ^ (rcon << 24)
        for _ in range(4):
            t ^= w[-4]
            w.append(t)
    return w


def _encrypt(w: list[int], block: int) -> int:
    s0 = (block >> 96) ^ w[0]
    s1 = ((block >> 64) & 0xFFFFFFFF) ^ w[1]
    s2 = ((block >> 32) & 0xFFFFFFFF) ^ w[2]
    s3 = (block & 0xFFFFFFFF) ^ w[3]
    for r in range(4, 40, 4):
        s0, s1, s2, s3 = (
            _T0[s0 >> 24] ^ _T1[(s1 >> 16) & 0xFF] ^ _T2[(s2 >> 8) & 0xFF] ^ _T3[s3 & 0xFF] ^ w[r],
            _T0[s1 >> 24] ^ _T1[(s2 >> 16) & 0xFF] ^ _T2[(s3 >> 8) & 0xFF] ^ _T3[s0 & 0xFF] ^ w[r + 1],
            _T0[s2 >> 24] ^ _T1[(s3 >> 16) & 0xFF] ^ _T2[(s0 >> 8) & 0xFF] ^ _T3[s1 & 0xFF] ^ w[r + 2],
            _T0[s3 >> 24] ^ _T1[(s0 >> 16) & 0xFF] ^ _T2[(s1 >> 8) & 0xFF] ^ _T3[s2 & 0xFF] ^ w[r + 3],
        )
    out = 0
    for a, b, c, d, k in ((s0, s1, s2, s3, w[40]), (s1, s2, s3, s0, w[41]),
                          (s2, s3, s0, s1, w[42]), (s3, s0, s1, s2, w[43])):
        word = ((_SBOX[a >> 24] << 24) | (_SBOX[(b >> 16) & 0xFF] << 16)
                | (_SBOX[(c >> 8) & 0xFF] << 8) | _SBOX[d & 0xFF]) ^ k
        out = (out << 32) | word
    return out


def _double(value: int) -> int:
    shifted = (value << 1) & _MASK_128
    return shifted ^ _CMAC_RB if value >> 127 else shifted


def aes128_encrypt(key: bytes, block: bytes) -> bytes:
    """AES-128 encryption of one 16-byte block."""
    return _encrypt(_expand_key(key), int.from_bytes(block, "big")).to_bytes(_BLOCK_LEN, "big")


def aes_cmac(key: bytes, message: bytes) -> bytes:
    """AES-128-CMAC tag (RFC 4493), 16 bytes."""
    w = _expand_key(key)
    k1 = _double(_encrypt(w, 0))
    tail_len = len(message) % _BLOCK_LEN
    if message and tail_len == 0:
        body, last = message[:-_BLOCK_LEN], int.from_bytes(message[-_BLOCK_LEN:], "big") ^ k1
    else:
        tail = message[len(message) - tail_len:] + b"\x80" + bytes(_BLOCK_LEN - 1 - tail_len)
        body, last = message[:len(message) - tail_len], int.from_bytes(tail, "big") ^ _double(k1)
    x = 0
    for i in range(0, len(body), _BLOCK_LEN):
        x = _encrypt(w, x ^ int.from_bytes(body[i:i + _BLOCK_LEN], "big"))
    return _encrypt(w, x ^ last).to_bytes(_BLOCK_LEN, "big")
