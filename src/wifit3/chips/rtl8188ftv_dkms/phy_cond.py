"""phydm conditional-table walker for the 8188F init tables.

Ported from ``ODM_ReadAndConfig_MP_8188F_*`` (hal/phydm/rtl8188f/
halhwimg8188f_{mac,bb,rf}.c) + ``CheckPositive`` (halhwimg8188f_mac.c).
Flat-u32 tables interleave (addr, value) rows with condition control words;
``walk_table`` takes an ``emit(addr, value)`` callback per table family.

Driver words for this card: cut=1 (SYS_CFG), platform=ODM_CE(0x04),
interface=ODM_ITRF_USB(0x02), package=7 (hidden-report byte 4 bits[6:4]),
board=0 (the 8188F HAL never sets ExternalLNA/PA or BT). TypeGLNA/GPA/ALNA/APA
are likewise never set, so driver2/3/4 are 0 unconditionally.
"""
from __future__ import annotations

from typing import Callable

_BIT31, _BIT30, _BIT29, _BIT28 = 1 << 31, 1 << 30, 1 << 29, 1 << 28
_COND_ELSE, _COND_ENDIF = 2, 3

DRIVER1 = (0x01 << 24) | (0x04 << 16) | (0x07 << 12) | (0x02 << 8)
DRIVER2 = DRIVER3 = DRIVER4 = 0


def check_positive(cond1: int, cond2: int, cond4: int,
                   driver1: int = DRIVER1, driver2: int = DRIVER2,
                   driver4: int = DRIVER4) -> bool:
    if (cond1 & 0x0000F000) and (cond1 & 0x0000F000) != (driver1 & 0x0000F000):
        return False
    if (cond1 & 0x0F000000) and (cond1 & 0x0F000000) != (driver1 & 0x0F000000):
        return False
    cond1 &= 0x00FF0FFF
    driver1 &= 0x00FF0FFF
    if (cond1 & driver1) != cond1:
        return False
    if (cond1 & 0x0F) == 0:
        return True
    bit_mask = 0
    if cond1 & (1 << 0):
        bit_mask |= 0x000000FF
    if cond1 & (1 << 1):
        bit_mask |= 0x0000FF00
    if cond1 & (1 << 2):
        bit_mask |= 0x00FF0000
    if cond1 & (1 << 3):
        bit_mask |= 0xFF000000
    return ((cond2 & bit_mask) == (driver2 & bit_mask)
            and (cond4 & bit_mask) == (driver4 & bit_mask))


def walk_table(table, emit: Callable[[int, int], None],
               driver1: int | None = None, driver2: int | None = None,
               driver4: int | None = None) -> None:
    d1 = DRIVER1 if driver1 is None else driver1
    d2 = DRIVER2 if driver2 is None else driver2
    d4 = DRIVER4 if driver4 is None else driver4
    i, n = 0, len(table)
    is_matched, is_skipped = True, False
    pre_v1 = pre_v2 = 0
    while i + 1 < n:
        v1, v2 = table[i], table[i + 1]
        if v1 & (_BIT31 | _BIT30):
            if v1 & _BIT31:
                c_cond = (v1 & (_BIT29 | _BIT28)) >> 28
                if c_cond == _COND_ENDIF:
                    is_matched, is_skipped = True, False
                elif c_cond == _COND_ELSE:
                    is_matched = not is_skipped
                else:
                    pre_v1, pre_v2 = v1, v2
            elif v1 & _BIT30:
                if not is_skipped:
                    if check_positive(pre_v1, pre_v2, v2, d1, d2, d4):
                        is_matched, is_skipped = True, True
                    else:
                        is_matched, is_skipped = False, False
                else:
                    is_matched = False
        else:
            if is_matched:
                emit(v1, v2)
        i += 2
