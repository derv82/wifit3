"""RTL8822CU read-only chip identity decode.

Mirrors HALMAC's mount-time ``SYS_CFG2`` chip-id check and the common
``SYS_CFG1`` / ``SYS_STATUS1`` bit-field decoding used by the vendor RTL8822CU
driver. These reads are deliberately side-effect free.
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    BIT_RF_TYPE_ID,
    CHIP_CUT_MASK,
    CHIP_CUT_SHIFT,
    REG_SYS_CFG1,
    REG_SYS_CFG2,
    REG_SYS_STATUS1,
    REG_WL_BT_PWR_CTRL,
    ROM_VERSION_MASK,
    ROM_VERSION_SHIFT,
)


@dataclass(frozen=True)
class ChipInfo:
    chip_id: int
    cut: int
    rf_2t2r: bool
    rom_version: int
    raw_cfg1: int
    raw_cfg2: int
    raw_status1: int
    chip_ver: int
    raw_multifunc: int


def read_chip_info(transport) -> ChipInfo:
    """Read the immutable RTL8822CU identity registers in the vendor's order.

    HALMAC ``get_chip_info`` (USB) reads the chip-id and chip-ver bytes at mount
    [SRC hal/halmac/halmac_api.c:532-534], then core ``read_chip_version`` reads the
    SYS_CFG1 / SYS_STATUS1 / WL_BT_PWR_CTRL dwords [SRC hal/rtl8822c/rtl8822c_ops.c:179-207].
    """
    chip_id = transport.read8(REG_SYS_CFG2)              # 0xFC
    chip_ver = transport.read8(REG_SYS_CFG1 + 1) >> 4    # 0xF1
    cfg1 = transport.read32(REG_SYS_CFG1)                # 0xF0
    status1 = transport.read32(REG_SYS_STATUS1)          # 0xF4
    multifunc = transport.read32(REG_WL_BT_PWR_CTRL)     # 0x68
    return ChipInfo(
        chip_id=chip_id,
        cut=(cfg1 >> CHIP_CUT_SHIFT) & CHIP_CUT_MASK,
        rf_2t2r=bool(cfg1 & BIT_RF_TYPE_ID),
        rom_version=(status1 >> ROM_VERSION_SHIFT) & ROM_VERSION_MASK,
        raw_cfg1=cfg1,
        raw_cfg2=chip_id,
        raw_status1=status1,
        chip_ver=chip_ver,
        raw_multifunc=multifunc,
    )
