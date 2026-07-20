"""WPS PIN keyspace — the two-halves search with checksum.

An 8-digit WPS PIN is P1 (first 4 digits) + P2 (last 4 = 3 free digits + a
checksum over the first 7). The AP judges each half separately, letting us recover them
independently: 10⁴ first-half guesses, then 10³ second-half guesses (the 8th
digit is derived), ≈ 11,000 worst case instead of 10⁸.

During the first-half sweep the second half is irrelevant to the M4 R-Hash1
check, so we pad it with a fixed valid tail. Once the first half is confirmed
(the AP returns M5), the second-half sweep walks the 1000 valid tails.
"""

from __future__ import annotations

from typing import List, Tuple

from wifit3.dot11.wsc.crypto import pin_checksum

# Tried first, before the brute sweep — the canonical/default PINs that hit a
# surprising number of consumer routers. (Full 8-digit, checksum-valid.)
COMMON_PINS: List[str] = [
    "12345670",   # the WSC spec example / many demos
    "00000000",
    "12345678",   # not checksum-valid but some firmwares accept it literally
    "11111111",
    "20172527",   # D-Link family
    "28296607",   # Belkin family
    "88888888",
    "10000005",
]


def full_pin(first4: str, middle3: str) -> str:
    """Assemble an 8-digit PIN from a 4-digit first half + 3 free digits,
    appending the computed checksum as the 8th digit."""
    seven = first4 + middle3
    return seven + str(pin_checksum(int(seven)))


def split_pin(pin: str) -> Tuple[str, str]:
    """('01030365') -> ('0103', '0365')."""
    return pin[:4], pin[4:]
