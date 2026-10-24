"""WPA passphrases: length validation, SSID variants, wordlist streaming."""
from collections.abc import Iterable, Iterator
from pathlib import Path

DEFAULT_PASSWORDS_PATH = Path(__file__).parent / "assets" / "default_passwords.txt"
SSID_VARIANTS_VERSION = 1

# ssid_passwords() fans each probed SSID out to 3 cases x (1 + len(_SSID_SUFFIXES)) = up to 15
# passwords, each one PBKDF2 (~2 ms on a Pi core), and caps the total.
_SSID_SUFFIXES = ("123", "1234", "12345678", "!")
_MAX_SSID_PASSWORDS = 1500
_COMMON_SSID_SUFFIXES = (
    "1", "12", "12345", "123456", "1234567", "0", "00", "000", "0000",
    "1111", "111111", "11111111", "66666666", "88888888", "password",
    "wifi", "admin", "home", "@", "#", "!!", "!123", "@123",
)
_RECENT_YEAR_SUFFIXES = ("2026", "2025", "2024")
_SEPARATORS = ("_", "-", "@")
_SEPARATOR_SUFFIXES = _SSID_SUFFIXES + ("123456",) + _RECENT_YEAR_SUFFIXES
_LEET_SUFFIXES = ("1",) + _SSID_SUFFIXES
_LEET_REPLACEMENTS = {"a": "4", "e": "3", "i": "1", "o": "0", "s": "5"}
_MAX_NUMBER_DIGITS = 4


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


def _with_number(base: str, number: int) -> str:
    """``base`` + ``number``, zero-padded only as far as 8 bytes needs, to at most 4 digits."""
    return base + str(number).zfill(min(8 - len(base.encode()), _MAX_NUMBER_DIGITS))


def ssid_variants(ssid: str) -> list[str]:
    """Passwords built from one SSID: its cases, suffixes, leetspeak and numbers 0-9999."""
    bases = list(dict.fromkeys((ssid, ssid.lower(), ssid.upper())))
    candidates: list[str] = list(bases)
    for base in bases:
        for suffix in _SSID_SUFFIXES:
            candidates.append(base + suffix)
    for base in bases:
        for leet_base in _leet_variants(base):
            candidates.append(leet_base)
            for suffix in _LEET_SUFFIXES:
                candidates.append(leet_base + suffix)
    for suffix in _SEPARATOR_SUFFIXES:
        for base in bases:
            for separator in _SEPARATORS:
                candidates.append(base + separator + suffix)
    for suffix in _COMMON_SSID_SUFFIXES + _RECENT_YEAR_SUFFIXES:
        for base in bases:
            candidates.append(base + suffix)
    for number in range(10_000):
        for base in bases:
            candidates.append(_with_number(base, number))
    return [psk for psk in dict.fromkeys(candidates) if is_valid_psk(psk)]


def ssid_passwords(ssids: Iterable[str]) -> list[str]:
    """Every SSID as-is, then its cases and _SSID_SUFFIXES; deduped, stopping at
    _MAX_SSID_PASSWORDS."""
    unique = list(dict.fromkeys(ssids))
    candidates: list[str] = list(unique)
    for ssid in unique:
        bases = (ssid, ssid.lower(), ssid.upper())
        candidates.extend(bases)
        for base in bases:
            for suffix in _SSID_SUFFIXES:
                candidates.append(base + suffix)
    passwords: dict[str, None] = {}
    for psk in candidates:
        if len(passwords) == _MAX_SSID_PASSWORDS:
            break
        if is_valid_psk(psk):
            passwords[psk] = None
    return list(passwords)


def read_passwords(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8", errors="ignore") as wordlist:
        for line in wordlist:
            psk = line.rstrip("\r\n")
            if is_valid_psk(psk):
                yield psk
