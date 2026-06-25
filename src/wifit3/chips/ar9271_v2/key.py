"""Key cache clear at bring-up — ported from ath/key.c + ath9k/common.c.

Some parts don't reset the key cache on power-up, so cold init zeroes all 128 entries.
The TKIP-companion (mic-entry) clear is ported behind its runtime check but never runs at
cold boot (no entry is TKIP-typed yet).
"""
from __future__ import annotations

from . import reg as R
from .hw import AthHw


def keyreset(hw: AthHw, entry: int, mic_combined: bool = False) -> None:
    """ath_hw_keyreset [SRC] ath/key.c:42 — read the entry type, then zero the 5 key words,
    set TYPE=CLR, and zero the MAC words, as one buffered batch."""
    base = R.AR_KEYTABLE(entry)
    key_type = hw.read(base + 20)            # AR_KEYTABLE_TYPE(entry)

    hw.enable_write_buffer()
    for off in (0, 4, 8, 12, 16):            # AR_KEYTABLE_KEY0..KEY4
        hw.write(base + off, 0)
    hw.write(base + 20, R.AR_KEYTABLE_TYPE_CLR)
    hw.write(base + 24, 0)                   # AR_KEYTABLE_MAC0
    hw.write(base + 28, 0)                   # AR_KEYTABLE_MAC1

    if key_type == R.AR_KEYTABLE_TYPE_TKIP:  # untested at cold boot — no TKIP entries yet
        mic = R.AR_KEYTABLE(entry + 64)
        for off in (0, 4, 8, 12):
            hw.write(mic + off, 0)
        if mic_combined:
            hw.write(mic + 16, 0)
            hw.write(mic + 20, R.AR_KEYTABLE_TYPE_CLR)

    hw.write_flush()


def init_crypto(hw: AthHw) -> None:
    """ath9k_cmn_init_crypto [SRC] ath9k/common.c:381 — reset the whole key cache."""
    for i in range(R.AR_KEYTABLE_SIZE):
        keyreset(hw, i)
