"""Runtime walker for rtw88 phy_cond tables — mirrors phy.c:rtw_parse_tbl_phy_cond.

A phy_cond table is a flat list of u32, walked 2 at a time. Each 2-u32 group
is a `union phy_table_tile`, interpreted as either:

  * a `phy_cond` marker (pos bit set)    — IF / ELIF / ELSE / ENDIF
  * a `neg-trigger`     (neg bit set)    — pairs with previous IF / ELIF
  * a `cfg pair`        (neither set)    — addr, data → write

The 32-bit cond word layout (struct rtw_phy_cond, little-endian) is:

    rfe       : 8   bits 0..7
    intf      : 4   bits 8..11
    pkg       : 4   bits 12..15
    plat      : 4   bits 16..19
    intf_rsvd : 4   bits 20..23
    cut       : 4   bits 24..27
    branch    : 2   bits 28..29   (0=IF, 1=ELIF, 2=ELSE, 3=ENDIF)
    neg       : 1   bit 30
    pos       : 1   bit 31

The cond2 word (struct rtw_phy_cond2) is 4 bytes, used only when `rfe & 0x0f`:

    type_glna : 8   bits 0..7
    type_gpa  : 8   bits 8..15
    type_alna : 8   bits 16..23
    type_apa  : 8   bits 24..31

References:
    data_dumps/rtw88-source-v6.18/phy.c:1150  check_positive
    data_dumps/rtw88-source-v6.18/phy.c:1193  rtw_parse_tbl_phy_cond
    data_dumps/rtw88-source-v6.18/main.h:1845 struct rtw_phy_cond
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

INTF_USB = 1
INTF_PCIE = 2
INTF_SDIO = 4

BRANCH_IF = 0
BRANCH_ELIF = 1
BRANCH_ELSE = 2
BRANCH_ENDIF = 3

RTW_CHIP_TYPE_8821A = 8
RTW_CHIP_TYPE_8812A = 9


@dataclass(frozen=True)
class PhyCond:
    rfe: int = 0
    intf: int = 0
    pkg: int = 0
    plat: int = 0
    cut: int = 0
    branch: int = 0
    neg: int = 0
    pos: int = 0

    @classmethod
    def decode(cls, word: int) -> "PhyCond":
        return cls(
            rfe=word & 0xFF,
            intf=(word >> 8) & 0x0F,
            pkg=(word >> 12) & 0x0F,
            plat=(word >> 16) & 0x0F,
            cut=(word >> 24) & 0x0F,
            branch=(word >> 28) & 0x03,
            neg=(word >> 30) & 1,
            pos=(word >> 31) & 1,
        )


@dataclass(frozen=True)
class PhyCond2:
    type_glna: int = 0
    type_gpa: int = 0
    type_alna: int = 0
    type_apa: int = 0

    @classmethod
    def decode(cls, word: int) -> "PhyCond2":
        return cls(
            type_glna=word & 0xFF,
            type_gpa=(word >> 8) & 0xFF,
            type_alna=(word >> 16) & 0xFF,
            type_apa=(word >> 24) & 0xFF,
        )


@dataclass(frozen=True)
class DeviceCond:
    """The runtime side of the comparison — built from EFUSE/HCI.

    Mirrors `rtw_phy_setup_phy_cond` (phy.c:1103) for the 8821A/8812A path.
    """
    cut: int                 # hal->cut_version; default 15 if unknown
    pkg: int                 # default 15 if unknown
    intf: int                # INTF_USB for our purposes
    rfe: int                 # bit0=ext_lna_2g, bit1=ext_pa_2g, bit2=ext_lna_5g,
                             #   bit3=ext_pa_5g, bit4=btcoex
    cond2: PhyCond2          # only matters when rfe&0x0f != 0


def _check_positive(pos_cond: PhyCond, pos_cond2: PhyCond2,
                    dev: DeviceCond, *, chip_id: int) -> bool:
    """Mirrors phy.c:check_positive."""
    if pos_cond.cut and pos_cond.cut != dev.cut:
        return False
    if pos_cond.pkg and pos_cond.pkg != dev.pkg:
        return False
    if pos_cond.intf and pos_cond.intf != dev.intf:
        return False

    if chip_id in (RTW_CHIP_TYPE_8812A, RTW_CHIP_TYPE_8821A):
        # 8821A / 8812A: rfe&0x0f means "must match these flags",
        # and the cond2 type fields are checked per-flag.
        if not (pos_cond.rfe & 0x0F):
            return True
        if (pos_cond.rfe & dev.rfe) != pos_cond.rfe:
            return False
        if (pos_cond.rfe & 0x01) and pos_cond2.type_glna != dev.cond2.type_glna:
            return False
        if (pos_cond.rfe & 0x02) and pos_cond2.type_gpa != dev.cond2.type_gpa:
            return False
        if (pos_cond.rfe & 0x04) and pos_cond2.type_alna != dev.cond2.type_alna:
            return False
        if (pos_cond.rfe & 0x08) and pos_cond2.type_apa != dev.cond2.type_apa:
            return False
    else:
        if pos_cond.rfe != dev.rfe:
            return False
    return True


CfgCallback = Callable[[int, int], None]


def parse_tbl_phy_cond(
    table: list[int],
    dev: DeviceCond,
    do_cfg: CfgCallback,
    *,
    chip_id: int = RTW_CHIP_TYPE_8821A,
) -> int:
    """Walk a flat u32 table, invoking do_cfg(addr, data) for matched cfg rows.

    Returns the number of cfg rows dispatched.
    """
    n = len(table)
    if n & 1:
        raise ValueError(f"phy_cond table length must be even, got {n}")

    pos_cond = PhyCond()
    pos_cond2 = PhyCond2()
    is_matched = True
    is_skipped = False
    dispatched = 0

    i = 0
    while i < n:
        w0 = table[i]
        w1 = table[i + 1]
        i += 2
        cond = PhyCond.decode(w0)

        if cond.pos:
            if cond.branch == BRANCH_ENDIF:
                is_matched = True
                is_skipped = False
            elif cond.branch == BRANCH_ELSE:
                is_matched = False if is_skipped else True
            else:  # BRANCH_IF or BRANCH_ELIF
                pos_cond = cond
                pos_cond2 = PhyCond2.decode(w1)
        elif cond.neg:
            if not is_skipped:
                if _check_positive(pos_cond, pos_cond2, dev, chip_id=chip_id):
                    is_matched = True
                    is_skipped = True
                else:
                    is_matched = False
                    is_skipped = False
            else:
                is_matched = False
        elif is_matched:
            do_cfg(w0, w1)
            dispatched += 1

    return dispatched
