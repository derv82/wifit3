"""RTL8822BU M0 — chip identification (the bring-up's opening reads).

Two reads in two layers, both at probe time:
- ``get_chip_info`` — HALMAC's chip-id/cut detection, the very first register IO
  (REG_SYS_CFG2 -> chip_id, REG_SYS_CFG1+1>>4 -> cut). [SRC] halmac_api.c:517-521
- ``read_chip_version`` — the rtw-core chip-info read (three 32-bit reads). The
  decoded fields feed later BB/RF init, so the raw words are kept and decoded when
  a downstream milestone needs them. [SRC] hal/rtl8822b/rtl8822b_ops.c:173
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import REG_SYS_CFG1, REG_SYS_STATUS1, REG_WL_BT_PWR_CTRL, REG_SYS_CFG2

# [SRC] halmac_api.c:525-536 / halmac_type.h:64-65
CHIP_ID_HW_DEF_8821C = 0x09
CHIP_ID_HW_DEF_8822B = 0x0A
# [SRC] halmac_type.h:563-572 — HALMAC_CHIP_VER_*_CUT (A=0..). This card is D-cut.
CHIP_VER_D_CUT = 0x03


@dataclass
class ChipInfo:
    chip_id: int           # REG_SYS_CFG2 (raw); 0x0A == 8822B
    chip_ver: int          # REG_SYS_CFG1+1 >> 4; 0x03 == D-cut


def get_chip_info(t) -> ChipInfo:
    """HALMAC get_chip_info (USB branch) — the first register IO of the bring-up.
    [SRC] hal/halmac/halmac_api.c:517-521."""
    chip_id = t.read8(REG_SYS_CFG2)               # 0xFC
    chip_ver = t.read8(REG_SYS_CFG1 + 1) >> 4     # 0xF1
    return ChipInfo(chip_id=chip_id, chip_ver=chip_ver)


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
