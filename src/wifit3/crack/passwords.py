"""WPA passphrases: length validation, SSID variants, wordlist streaming."""
from collections.abc import Iterable, Iterator
from itertools import chain
from pathlib import Path

DEFAULT_PASSWORDS_PATH = Path(__file__).parent / "assets" / "default_passwords.txt"

# Each SSID fans out to 3 cases x (1 + len(_SSID_SUFFIXES)) = up to 15 passwords, each one PBKDF2
# (~2 ms on a Pi core). ssid_passwords() caps the total, so a long probe list stays bounded.
_SSID_SUFFIXES = ("123", "1234", "12345678", "!")
_MAX_SSID_PASSWORDS = 1500


def is_valid_psk(psk: str) -> bool:
    """8..63 bytes once UTF-8 encoded, matching how ``derive_pmk`` encodes the passphrase."""
    return 8 <= len(psk.encode()) <= 63


def ssid_variants(ssid: str) -> list[str]:
    bases = [ssid, ssid.lower(), ssid.upper()]
    variants = bases + [base + suffix for base in bases for suffix in _SSID_SUFFIXES]
    return [psk for psk in dict.fromkeys(variants) if is_valid_psk(psk)]


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
