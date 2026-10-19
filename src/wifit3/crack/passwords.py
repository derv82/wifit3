"""WPA passphrases: length validation, SSID variants, wordlist streaming."""
from collections.abc import Iterable, Iterator
from itertools import chain
from pathlib import Path

DEFAULT_PASSWORDS_PATH = Path(__file__).parent / "assets" / "default_passwords.txt"
SSID_PASSWORDS_VERSION = 1
SSID_PASSWORDS_IDENTITY = f"ssid-passwords:v{SSID_PASSWORDS_VERSION}"


_PRIMARY_SSID_SUFFIXES = ("123", "1234", "12345678", "!")
_COMMON_SSID_SUFFIXES = (
    "1", "12", "12345", "123456", "1234567", "0", "00", "000", "0000",
    "1111", "111111", "11111111", "66666666", "88888888", "password",
    "wifi", "admin", "home", "@", "#", "!!", "!123", "@123",
)
_YEAR_SUFFIXES = tuple(str(year) for year in range(2030, 1989, -1))
_RECENT_YEAR_SUFFIXES = ("2026", "2025", "2024")
_SEPARATORS = ("_", "-", "@")
_SEPARATOR_SUFFIXES = (*_PRIMARY_SSID_SUFFIXES, "123456", *_RECENT_YEAR_SUFFIXES)
_LEET_REPLACEMENTS = {"a": "4", "e": "3", "i": "1", "o": "0", "s": "5"}
_SSID_VARIANT_LIMIT = 512
_MAX_SSID_PASSWORDS = 1500


def is_valid_psk(psk: str) -> bool:
    """8..63 bytes once UTF-8 encoded, matching how ``derive_pmk`` encodes the passphrase."""
    return 8 <= len(psk.encode()) <= 63


def _leet_variants(value: str) -> list[str]:
    variants: dict[str, None] = {}
    replaced = list(value)
    for index, character in enumerate(value):
        replacement = _LEET_REPLACEMENTS.get(character.lower())
        if replacement is not None:
            variants[value[:index] + replacement + value[index + 1:]] = None
            replaced[index] = replacement
    combined = "".join(replaced)
    if combined != value:
        variants[combined] = None
    return list(variants)[:16]


def _numeric_codes() -> Iterator[str]:
    for number in range(10_000):
        for width in range(1, 5):
            if number < 10 ** width:
                yield f"{number:0{width}d}"


def ssid_variants(ssid: str) -> list[str]:
    bases = list(dict.fromkeys((ssid, ssid.lower(), ssid.upper())))
    primary = bases + [base + suffix for base in bases for suffix in _PRIMARY_SSID_SUFFIXES]
    leet_suffixes = ("1", *_PRIMARY_SSID_SUFFIXES)
    leet = (candidate for base in bases for leet_base in _leet_variants(base)
            for candidate in (leet_base, *(leet_base + suffix for suffix in leet_suffixes)))
    separated = (base + separator + suffix for suffix in _SEPARATOR_SUFFIXES
                 for base in bases for separator in _SEPARATORS)
    broader = (base + suffix for suffix in (*_COMMON_SSID_SUFFIXES, *_RECENT_YEAR_SUFFIXES)
               for base in bases)
    numeric = (base + code for code in _numeric_codes() for base in bases)
    older_years = (base + suffix for suffix in _YEAR_SUFFIXES
                   if suffix not in _RECENT_YEAR_SUFFIXES for base in bases)
    variants: dict[str, None] = {}
    for candidate in chain(primary, leet, separated, broader, numeric, older_years):
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
