"""RTL88xxAU JaguarSeries phy_cond table walker (BB / AGC / RF init tables).

The vendor BB/AGC/RF tables are flat u32 streams with embedded condition rows
(IF / ELSE_IF / ELSE / ENDIF) that ODM_ReadAndConfig resolves at apply time. This
ports the JaguarSeries 4-condition walker + check_positive 1:1 from
`halhwimg8821a_bb.c:21-106` (check_positive) and `:366-415` (the walker loop,
identical across the three tables and shared by the 8812a tables). ``apply_table``
emits write_fn(addr, value) for each taken data row.

For a default board every block resolves to its default / ELSE branch
(board_type=0, cut=0, USB, CE) — but the walker handles every branch, so an external-PA/LNA
card would derive correctly from its own efuse-decoded params.
"""
from __future__ import annotations

from dataclasses import dataclass

COND_ELSE = 2       # [SRC] phydm_types.h:385
COND_ENDIF = 3      # [SRC] phydm_types.h:386
_BIT30 = 1 << 30
_BIT31 = 1 << 31


@dataclass
class JaguarParams:
    """Condition inputs for check_positive. Defaults are a plain USB/CE board."""
    cut_version: int = 0
    support_interface: int = 0x02   # ODM_ITRF_USB
    support_platform: int = 0x04    # ODM_CE
    package_type: int = 0
    board_type: int = 0
    type_glna: int = 0
    type_gpa: int = 0
    type_alna: int = 0
    type_apa: int = 0


def _check_positive(p: JaguarParams, cond1: int, cond2: int, cond3: int, cond4: int) -> bool:
    _board_type = (((p.board_type & (1 << 4)) >> 4) << 0          # GLNA
                   | ((p.board_type & (1 << 3)) >> 3) << 1        # GPA
                   | ((p.board_type & (1 << 7)) >> 7) << 2        # ALNA
                   | ((p.board_type & (1 << 6)) >> 6) << 3        # APA
                   | ((p.board_type & (1 << 2)) >> 2) << 4)       # BT
    driver1 = ((p.cut_version << 24)
               | ((p.support_interface & 0xF0) << 16)
               | (p.support_platform << 16)
               | (p.package_type << 12)
               | ((p.support_interface & 0x0F) << 8)
               | _board_type)
    driver2 = ((p.type_glna & 0xFF) | ((p.type_gpa & 0xFF) << 8)
               | ((p.type_alna & 0xFF) << 16) | ((p.type_apa & 0xFF) << 24))
    driver4 = (((p.type_glna & 0xFF00) >> 8) | (p.type_gpa & 0xFF00)
               | ((p.type_alna & 0xFF00) << 8) | ((p.type_apa & 0xFF00) << 16))

    # Value-defined check: QFN [15:12] and cut [27:24].
    if (cond1 & 0xF000) != 0 and (cond1 & 0xF000) != (driver1 & 0xF000):
        return False
    if (cond1 & 0x0F000000) != 0 and (cond1 & 0x0F000000) != (driver1 & 0x0F000000):
        return False
    # Bit-defined check ([31:28] don't care).
    cond1 &= 0x00FF0FFF
    driver1 &= 0x00FF0FFF
    if (cond1 & driver1) != cond1:
        return False
    if (cond1 & 0x0F) == 0:          # board_type DONTCARE
        return True
    bit_mask = 0
    if cond1 & (1 << 0):
        bit_mask |= 0x000000FF       # GLNA
    if cond1 & (1 << 1):
        bit_mask |= 0x0000FF00       # GPA
    if cond1 & (1 << 2):
        bit_mask |= 0x00FF0000       # ALNA
    if cond1 & (1 << 3):
        bit_mask |= 0xFF000000       # APA
    return (cond2 & bit_mask) == (driver2 & bit_mask) and (cond4 & bit_mask) == (driver4 & bit_mask)


def apply_table(array, write_fn, params: JaguarParams) -> None:
    """Walk a JaguarSeries u32 table; call write_fn(addr, value) for taken rows."""
    is_matched, is_skipped = True, False
    pre_v1 = pre_v2 = 0
    i, n = 0, len(array)
    while i + 1 < n:
        v1, v2 = array[i], array[i + 1]
        if v1 & (_BIT31 | _BIT30):
            if v1 & _BIT31:                                  # IF / ELSE_IF / ELSE / ENDIF
                c_cond = (v1 & ((1 << 29) | (1 << 28))) >> 28
                if c_cond == COND_ENDIF:
                    is_matched, is_skipped = True, False
                elif c_cond == COND_ELSE:
                    is_matched = False if is_skipped else True
                else:                                        # IF / ELSE_IF: stash conditions
                    pre_v1, pre_v2 = v1, v2
            elif v1 & _BIT30:                                # the paired negative condition
                if not is_skipped:
                    if _check_positive(params, pre_v1, pre_v2, v1, v2):
                        is_matched, is_skipped = True, True
                    else:
                        is_matched, is_skipped = False, False
                else:
                    is_matched = False
        elif is_matched:                                     # data row
            write_fn(v1, v2)
        i += 2
