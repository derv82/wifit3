"""phydm conditional-table walker — shared by the MAC, BB, AGC and RF init tables.

The flat-u32 tables interleave (addr, value) data rows with condition control
words: a positive word (BIT31, IF/ELSE-IF/ELSE/ENDIF selector in bits[29:28])
optionally followed by a negative word (BIT30). Data rows fire only while the
current IF branch matches the chip. [SRC] halhwimg8188e_mac.c
odm_read_and_config_mp_8188e_mac_reg + check_positive.

The 8188e ``check_positive`` matches four condition words against four driver
words (driver1 = cut/platform/interface/package/board_type; driver2/4 = the
per-path LNA/PA types). The data-row action differs per table family (MAC write8,
BB write32, RF SIPI write), so ``walk_table`` takes an ``emit(addr, value)``
callback rather than touching the transport directly.
"""
from __future__ import annotations

from typing import Callable

_BIT31, _BIT30, _BIT29, _BIT28 = 1 << 31, 1 << 30, 1 << 29, 1 << 28
_COND_ELSE, _COND_ENDIF = 2, 3  # [SRC] phydm_types.h:236 (IF=0, ELSE_IF=1)

# This card (TL-WN722N v2): cut=ODM_CUT_A(0), platform=ODM_CE(0x04),
# interface=ODM_ITRF_USB(0x02), package=0, board_type=ODM_BOARD_DEFAULT(0) — a
# plain board with no external LNA/PA/BT (the efuse ExternalLNA/PA flags are 0,
# confirmed by the wire taking every board-gated table branch's ELSE default).
# [SRC] hal/hal_dm.c Init_ODM_ComInfo + rtl8188e_dm.c Init_ODM_ComInfo_88E.
_CUT_VERSION = 0x00       # ODM_CUT_A
_SUPPORT_PLATFORM = 0x04  # ODM_CE
_SUPPORT_INTERFACE = 0x02  # ODM_ITRF_USB
_PACKAGE_TYPE = 0x00
_BOARD_TYPE = 0x00        # ODM_BOARD_DEFAULT

# driver2/3/4 carry the per-path LNA/PA type fields; all zero on this plain board.
DRIVER2 = DRIVER3 = DRIVER4 = 0


def build_driver1() -> int:
    """[SRC] check_positive preamble — assemble the match word from chip params."""
    return (
        (_CUT_VERSION & 0xFF) << 24
        | (_SUPPORT_INTERFACE & 0xF0) << 16
        | _SUPPORT_PLATFORM << 16
        | (_PACKAGE_TYPE & 0xF) << 12
        | (_SUPPORT_INTERFACE & 0x0F) << 8
        | (_BOARD_TYPE & 0xFF)
    )


DRIVER1 = build_driver1()  # 0x00040200 on this card (internal PA+LNA, board_type 0)


def build_driver_words(external_lna_2g: bool, external_pa_2g: bool,
                       type_glna: int) -> tuple[int, int, int]:
    """Assemble (driver1, driver2, driver4) from the runtime efuse board options.
    [SRC] check_positive (halhwimg8188e_bb.c:30-54) + the ODM_CMNINFO feed
    (hal_dm.c:224-260). The board_type byte repacks to _board_type[0]=GLNA(ext-LNA
    2G), [1]=GPA(ext-PA 2G), [2]=ALNA, [3]=APA (both 5G, always 0 on 88EU), [4]=BT
    (no 88EU init table gates on it). type_gpa/alna/apa are never set on 88EU, so the
    only non-zero type field is type_glna (0x0/0x1/0x2) and driver4 is always 0.

    A default (internal PA+LNA, type_glna 0) returns exactly the module DRIVER* words,
    so the reference card walks byte-identically."""
    board_type = (int(bool(external_lna_2g)) << 0) | (int(bool(external_pa_2g)) << 1)
    driver1 = (DRIVER1 & ~0x1F) | board_type            # replace the 5 _board_type bits
    driver2 = type_glna & 0xFF                           # (type_glna) | gpa<<8 | ... (all 0)
    driver4 = (type_glna & 0xFF00) >> 8                  # 0 for TypeGLNA <= 0x2
    return driver1, driver2, driver4


def check_positive(cond1: int, cond2: int, cond4: int,
                   driver1: int = DRIVER1, driver2: int = DRIVER2,
                   driver4: int = DRIVER4) -> bool:
    """[SRC] check_positive (halhwimg8188e_mac.c). cond3/driver3 are unused (0)."""
    # value-defined check: QFN package [15:12] and cut version [27:24]
    if (cond1 & 0x0000F000) and (cond1 & 0x0000F000) != (driver1 & 0x0000F000):
        return False
    if (cond1 & 0x0F000000) and (cond1 & 0x0F000000) != (driver1 & 0x0F000000):
        return False
    # bit-defined check ([31:28] don't-care)
    cond1 &= 0x00FF0FFF
    driver1 &= 0x00FF0FFF
    if (cond1 & driver1) != cond1:
        return False
    if (cond1 & 0x0F) == 0:        # board_type is DONT-CARE
        return True
    bit_mask = 0
    if cond1 & (1 << 0):           # GLNA
        bit_mask |= 0x000000FF
    if cond1 & (1 << 1):           # GPA
        bit_mask |= 0x0000FF00
    if cond1 & (1 << 2):           # ALNA
        bit_mask |= 0x00FF0000
    if cond1 & (1 << 3):           # APA
        bit_mask |= 0xFF000000
    return ((cond2 & bit_mask) == (driver2 & bit_mask)
            and (cond4 & bit_mask) == (driver4 & bit_mask))


def walk_table(table, emit: Callable[[int, int], None],
               driver1: int | None = None, driver2: int | None = None,
               driver4: int | None = None) -> None:
    """[SRC] odm_read_and_config_mp_8188e_* — call ``emit(addr, value)`` per taken row.
    The driver words gate the board/LNA-conditional branches; ``None`` uses the
    module defaults (internal PA+LNA, the reference card)."""
    d1 = DRIVER1 if driver1 is None else driver1
    d2 = DRIVER2 if driver2 is None else driver2
    d4 = DRIVER4 if driver4 is None else driver4
    i, n = 0, len(table)
    is_matched, is_skipped = True, False
    pre_v1 = pre_v2 = 0
    while i + 1 < n:
        v1, v2 = table[i], table[i + 1]
        if v1 & (_BIT31 | _BIT30):
            if v1 & _BIT31:                       # positive condition
                c_cond = (v1 & (_BIT29 | _BIT28)) >> 28
                if c_cond == _COND_ENDIF:
                    is_matched, is_skipped = True, False
                elif c_cond == _COND_ELSE:
                    is_matched = not is_skipped
                else:                             # IF / ELSE IF — remember the words
                    pre_v1, pre_v2 = v1, v2
            elif v1 & _BIT30:                     # negative condition (pairing word)
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
