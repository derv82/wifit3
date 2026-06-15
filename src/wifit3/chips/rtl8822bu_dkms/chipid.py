"""RTL8822BU M0 — chip-version read (the bring-up's opening reads).

Port of rtl8822b_ops.c:read_chip_version (the rtw-core chip-info read run at
probe). On the wire it is three 32-bit reads; the decoded fields (cut version,
vendor, RF type, ROM ver, multifunc) feed later BB/RF init, so the raw words are
kept here and decoded when a downstream milestone needs them.
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import REG_SYS_CFG1, REG_SYS_STATUS1, REG_WL_BT_PWR_CTRL


@dataclass
class ChipVersion:
    sys_cfg1: int          # REG_SYS_CFG1: ICType/ChipType/CUTVersion/Vendor/RFType/Regulator
    sys_status1: int       # REG_SYS_STATUS1: ROMVer
    wl_bt_pwr_ctrl: int    # REG_WL_BT_PWR_CTRL: MultiFunc/PolarityCtl


def read_chip_version(t) -> ChipVersion:
    """[SRC] hal/rtl8822b/rtl8822b_ops.c:173 read_chip_version."""
    sys_cfg1 = t.read32(REG_SYS_CFG1)
    sys_status1 = t.read32(REG_SYS_STATUS1)
    wl_bt_pwr_ctrl = t.read32(REG_WL_BT_PWR_CTRL)
    return ChipVersion(sys_cfg1, sys_status1, wl_bt_pwr_ctrl)
