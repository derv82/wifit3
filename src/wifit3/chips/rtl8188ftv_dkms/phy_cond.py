"""phydm conditional-table walker for the 8188F init tables.

Ported from ``ODM_ReadAndConfig_MP_8188F_*`` (hal/phydm/rtl8188f/
halhwimg8188f_{mac,bb,rf}.c) + ``CheckPositive`` (halhwimg8188f_mac.c:25-107).
Flat-u32 tables interleave (addr, value) rows with condition control words;
``walk_table`` takes an ``emit(addr, value)`` callback per table family.

driver1 sources: CutVersion = SYS_CFG cut (rtl8188f_dm.c:244-248),
PackageType = hidden-report byte 4 bits[6:4] (hal_com.c:829,879,915; rtl8188f_dm.c:241).
Platform ODM_CE and interface USB are fixed (hal_dm.c:89-96). BoardType is
ODM_BOARD_DEFAULT: nothing in the 8188F source sets ExternalLNA/PA or
EEPROMBluetoothCoexist (hal_dm.c:128-150). TypeGLNA/GPA/ALNA/APA are never
set either, so driver2/4 are 0.
"""
from __future__ import annotations

from typing import Callable

_BIT31, _BIT30, _BIT29, _BIT28 = 1 << 31, 1 << 30, 1 << 29, 1 << 28
_COND_ELSE, _COND_ENDIF = 2, 3

ODM_CE = 0x04               # hal/phydm/phydm_types.h:27
ODM_ITRF_USB = 0x2          # hal/phydm/phydm_pre_define.h:253 (RTW_USB = BIT1, hal_intf.h:26)
ODM_BOARD_DEFAULT = 0       # hal/phydm/phydm_pre_define.h:496
DRIVER2 = DRIVER4 = 0


def driver1(cut_version: int, package_type: int) -> int:
    return (cut_version << 24
            | (ODM_ITRF_USB & 0xF0) << 16
            | ODM_CE << 16
            | package_type << 12
            | (ODM_ITRF_USB & 0x0F) << 8
            | ODM_BOARD_DEFAULT)


def check_positive(cond1: int, cond2: int, cond4: int,
                   cut_version: int, package_type: int) -> bool:
    d1 = driver1(cut_version, package_type)
    if (cond1 & 0x0000F000) and (cond1 & 0x0000F000) != (d1 & 0x0000F000):
        return False
    if (cond1 & 0x0F000000) and (cond1 & 0x0F000000) != (d1 & 0x0F000000):
        return False
    cond1 &= 0x00FF0FFF
    d1 &= 0x00FF0FFF
    if (cond1 & d1) != cond1:
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
    return ((cond2 & bit_mask) == (DRIVER2 & bit_mask)
            and (cond4 & bit_mask) == (DRIVER4 & bit_mask))


def walk_table(table, emit: Callable[[int, int], None],
               cut_version: int, package_type: int) -> None:
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
                    if check_positive(pre_v1, pre_v2, v2, cut_version, package_type):
                        is_matched, is_skipped = True, True
                    else:
                        is_matched, is_skipped = False, False
                else:
                    is_matched = False
        else:
            if is_matched:
                emit(v1, v2)
        i += 2
