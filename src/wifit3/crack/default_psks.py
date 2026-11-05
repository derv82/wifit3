"""Default WPA passphrases that routers derive from their SSID and/or BSSID.

Each algorithm is ``(ssid, bssid) -> list[str]``, with ``bssid`` lowercase and colon-separated:
- Return ``[]`` unless its SSID pattern or BSSID prefix matches. Most APs match nothing.
- Match the exact MAC prefixes a scheme was seen on, not ``id/vendors.py``'s vendor name:
  a vendor has hundreds of OUIs, a scheme covers a few firmware lines.
- Output that isn't a WPA passphrase (WEP keys, under 8 or over 63 characters, raw 64-hex
  PMKs) is dropped by ``default_psks``.

Cost: ``all_ssid_variants`` runs every algorithm when the cracker modal opens (for its count)
and again when a job launches.
- Generation must be near-instant. Some schemes search a serial-number space and filter by
  the SSID (Thomson: 21.8M SHA-1s per SSID), far too slow for a modal.
- Every candidate costs one PBKDF2 (~2 ms on a Pi core). Most schemes give 1-10 keys; some
  give hundreds or thousands for one AP.

Changing an existing AP's candidates needs ``SSID_VARIANTS_VERSION`` (``crack/passwords.py``)
bumped, so APs whose variants already ran out get the new ones tried.

Known router schemes, their inputs and costs: ``docs/DEFAULT-PSKS.md``.
"""
from collections.abc import Callable

from wifit3.dot11.wpa import is_valid_psk

Algorithm = Callable[[str, str], list[str]]


def bssid_as_psk(ssid: str, bssid: str) -> list[str]:
    """The BSSID without colons, then its last 10 and last 8 hex digits, each upper and lower case."""
    compact = bssid.replace(":", "")
    return [psk for tail in (compact, compact[-10:], compact[-8:])
            for psk in (tail.upper(), tail.lower())]


ALGORITHMS: list[Algorithm] = [bssid_as_psk]


def default_psks(ssid: str, bssid: str) -> list[str]:
    """Every algorithm's valid WPA passphrases for this AP, deduped."""
    candidates = [psk for algorithm in ALGORITHMS for psk in algorithm(ssid, bssid)]
    return [psk for psk in dict.fromkeys(candidates) if is_valid_psk(psk)]
