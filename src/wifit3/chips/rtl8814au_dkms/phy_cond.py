"""phydm conditional-table walker — shared by the BB and RF init tables.

The PHY_REG / AGC_TAB / radio_{a,b,c,d} tables interleave (addr, value) data rows
with condition control words. A condition is a positive word (BIT31, with the
IF/ELSE-IF/ELSE/ENDIF selector in bits[29:28]) optionally followed by a negative
word (BIT30). Data rows fire only while the current IF branch matches the chip.
``check_positive`` compares the IF word's low 28 bits against ``driver1``.
[SRC] halhwimg8814a_bb.c check_positive:31 + odm_read_and_config_mp_8814a_*.

The data-row action differs per table family (BB write32, RF SIPI write, ...), so
``walk_table`` takes an ``emit(addr, value)`` callback rather than touching the
transport directly.
"""
from __future__ import annotations

from typing import Callable

_BIT31, _BIT30, _BIT29, _BIT28 = 1 << 31, 1 << 30, 1 << 29, 1 << 28
_COND_ELSE, _COND_ENDIF = 2, 3  # [SRC] phydm_types.h:301 (IF=0, ELSE_IF=1)

# This card: cut=A_CUT, package_type=0, interface=USB. check_positive folds
# cut==ODM_CUT_A -> 15 and package_type==0 -> 15 before building driver1; both are
# fixed for the 8814AU and (brute-forced) don't gate this card's taken path, so
# only rfe_type varies and it comes from efuse (efuse.read_chip_params). ODM_ITRF_USB
# =0x2, ODM_CE platform=0x8. [WIRE] driver1 0x0F08F201 (rfe=1) reproduces every BB
# and RF write; efuse independently decodes rfe_type=1 (verify_efuse_pcap.py).
_CUT_FOR_PARA = 0xF       # A_CUT -> 15
_PKG_FOR_PARA = 0xF       # package_type 0 -> 15
_SUPPORT_INTERFACE = 0x2  # ODM_ITRF_USB
_SUPPORT_PLATFORM = 0x8   # ODM_CE


def build_driver1(rfe_type: int) -> int:
    """[SRC] check_positive preamble — assemble the match word from chip params."""
    return (
        (_CUT_FOR_PARA & 0xFF) << 24
        | (_SUPPORT_INTERFACE & 0xF0) << 16
        | _SUPPORT_PLATFORM << 16
        | (_PKG_FOR_PARA & 0xF) << 12
        | (_SUPPORT_INTERFACE & 0x0F) << 8
        | (rfe_type & 0xFF)
    )


def check_positive(driver1: int, cond1: int) -> bool:
    """[SRC] check_positive — only cond1 (the IF word) is matched against driver1.

    Cut[27:24], package[15:12] and interface[11:8] nibbles are checked only when
    non-zero in the condition; the rfe byte[7:0] must always match. Bits[31:28]
    (the BIT31/30 + selector) are masked off by the nibble comparisons.
    """
    if (cond1 & 0x0F000000) and (cond1 & 0x0F000000) != (driver1 & 0x0F000000):
        return False
    if (cond1 & 0x0000F000) and (cond1 & 0x0000F000) != (driver1 & 0x0000F000):
        return False
    if (cond1 & 0x00000F00) and (cond1 & 0x00000F00) != (driver1 & 0x00000F00):
        return False
    return (cond1 & 0xFF) == (driver1 & 0xFF)


def walk_table(table, driver1: int, emit: Callable[[int, int], None]) -> None:
    """[SRC] odm_read_and_config_mp_8814a_* — call ``emit(addr, value)`` per taken row."""
    i, n = 0, len(table)
    is_matched, is_skipped, pre_v1 = True, False, 0
    while i + 1 < n:
        v1, v2 = table[i], table[i + 1]
        if v1 & (_BIT31 | _BIT30):
            if v1 & _BIT31:                       # positive condition
                c_cond = (v1 & (_BIT29 | _BIT28)) >> 28
                if c_cond == _COND_ENDIF:
                    is_matched, is_skipped = True, False
                elif c_cond == _COND_ELSE:
                    is_matched = not is_skipped
                else:                             # IF / ELSE IF
                    pre_v1 = v1
            elif v1 & _BIT30:                     # negative condition (pairing word)
                if not is_skipped:
                    if check_positive(driver1, pre_v1):
                        is_matched, is_skipped = True, True
                    else:
                        is_matched, is_skipped = False, False
                else:
                    is_matched = False
        else:
            if is_matched:
                emit(v1, v2)
        i += 2
