"""RTL8821AU phy_cond walker — re-export of the shared rtw88 walker.

The 8821A path uses the bitfield-rfe / cond2-type comparison rules. See
:mod:`wifit3.chips.rtw88_base.phy_cond` for the implementation.
"""

from __future__ import annotations

from wifit3.chips.rtw88_base.phy_cond import (
    BRANCH_ELIF,
    BRANCH_ELSE,
    BRANCH_ENDIF,
    BRANCH_IF,
    INTF_PCIE,
    INTF_SDIO,
    INTF_USB,
    RTW_CHIP_TYPE_8812A,
    RTW_CHIP_TYPE_8821A,
    CfgCallback,
    DeviceCond,
    PhyCond,
    PhyCond2,
    parse_tbl_phy_cond,
)

__all__ = [
    "BRANCH_ELIF",
    "BRANCH_ELSE",
    "BRANCH_ENDIF",
    "BRANCH_IF",
    "INTF_PCIE",
    "INTF_SDIO",
    "INTF_USB",
    "RTW_CHIP_TYPE_8812A",
    "RTW_CHIP_TYPE_8821A",
    "CfgCallback",
    "DeviceCond",
    "PhyCond",
    "PhyCond2",
    "parse_tbl_phy_cond",
]
