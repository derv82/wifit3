"""ar9002 PHY/analog bring-up over WMI.

Ported from ar9002_hw.c / ar9002_phy.c. Begins with the RF claim that __ath9k_hw_init runs
from ath9k_hw_post_init (not 9300+ path).
"""
from __future__ import annotations

from . import reg as R
from .hw import AthHw


def reverse_bits(val: int, n: int) -> int:
    """ath9k_hw_reverse_bits [SRC] hw.c:155 — MSB-first bit reversal of the low n bits."""
    retval = 0
    for _ in range(n):
        retval = (retval << 1) | (val & 1)
        val >>= 1
    return retval


def get_radiorev(hw: AthHw) -> int:
    """ar9002_hw_get_radiorev [SRC] ar9002_hw.c:324 — a buffered probe write then read the
    analog rev out of AR_PHY(256)."""
    hw.enable_write_buffer()
    hw.write(R.AR_PHY(0x36), 0x00007058)
    for _ in range(8):
        hw.write(R.AR_PHY(0x20), 0x00010000)
    hw.write_flush()

    val = (hw.read(R.AR_PHY(256)) >> 24) & 0xff
    val = ((val & 0xf0) >> 4) | ((val & 0x0f) << 4)
    return reverse_bits(val, 8)


def rf_claim(hw: AthHw) -> None:
    """ar9002_hw_rf_claim [SRC] ar9002_hw.c:343 — seed AR_PHY(0) and validate the radio rev."""
    hw.write(R.AR_PHY(0), 0x00000007)

    val = get_radiorev(hw)
    major = val & R.AR_RADIO_SREV_MAJOR
    if major == 0:
        val = R.AR_RAD5133_SREV_MAJOR
    elif major in (R.AR_RAD5133_SREV_MAJOR, R.AR_RAD5122_SREV_MAJOR,
                   R.AR_RAD2133_SREV_MAJOR, R.AR_RAD2122_SREV_MAJOR):
        pass
    else:
        raise RuntimeError(f"ar9271_v2: unsupported radio chip rev 0x{major:02x}")
    hw.analog5GhzRev = val
