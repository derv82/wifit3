"""WPA passphrases: length validation, SSID variants, wordlist streaming."""
from collections.abc import Iterable, Iterator
from itertools import chain
from pathlib import Path

DEFAULT_PASSWORDS_PATH = Path(__file__).parent / "assets" / "default_passwords.txt"


_PRIMARY_SSID_SUFFIXES = ("123", "1234", "12345678", "!")
_COMMON_SSID_SUFFIXES = (
    "1", "12", "12345", "123456", "1234567", "0", "00", "000", "0000",
    "1111", "111111", "11111111", "66666666", "88888888", "password",
    "wifi", "admin", "home", "@", "#", "!!", "!123", "@123",
)
_YEAR_SUFFIXES = tuple(str(year) for year in range(1990, 2031))
_TWO_DIGIT_SUFFIXES = tuple(f"{number:02d}" for number in range(100))
_SSID_VARIANT_LIMIT = 512
_MAX_SSID_PASSWORDS = 1500


def is_valid_psk(psk: str) -> bool:
    """8..63 bytes once UTF-8 encoded, matching how ``derive_pmk`` encodes the passphrase."""
    return 8 <= len(psk.encode()) <= 63


def ssid_variants(ssid: str) -> list[str]:
    bases = list(dict.fromkeys((ssid, ssid.lower(), ssid.upper())))
    primary = bases + [base + suffix for base in bases for suffix in _PRIMARY_SSID_SUFFIXES]
    broader_suffixes = (*_COMMON_SSID_SUFFIXES, *_YEAR_SUFFIXES, *_TWO_DIGIT_SUFFIXES)
    broader = (base + suffix for suffix in broader_suffixes for base in bases)
    variants: dict[str, None] = {}
    for candidate in chain(primary, broader):
        if is_valid_psk(candidate):
            variants[candidate] = None
            if len(variants) == _SSID_VARIANT_LIMIT:
                break
    return list(variants)


def ssid_passwords(ssids: Iterable[str]) -> list[str]:
    """Every SSID as-is, then their variants; deduped, stopping at _MAX_SSID_PASSWORDS."""
    unique = list(dict.fromkeys(ssids))
    passwords: dict[str, None] = {}
    for psk in chain((s for s in unique if is_valid_psk(s)),
                     (v for s in unique for v in ssid_variants(s))):
        if len(passwords) == _MAX_SSID_PASSWORDS:
            break
        passwords[psk] = None
    return list(passwords)


def read_passwords(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8", errors="ignore") as wordlist:
        for line in wordlist:
            psk = line.rstrip("\r\n")
            if is_valid_psk(psk):
                yield psk
