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


def read_chip_info(transport) -> ChipInfo:
    """Read the immutable RTL8822CU identity registers."""
    cfg1 = transport.read32(REG_SYS_CFG1)
    cfg2 = transport.read32(REG_SYS_CFG2)
    status1 = transport.read32(REG_SYS_STATUS1)
    return ChipInfo(
        chip_id=cfg2 & 0xFF,
        cut=(cfg1 >> CHIP_CUT_SHIFT) & CHIP_CUT_MASK,
        rf_2t2r=bool(cfg1 & BIT_RF_TYPE_ID),
        rom_version=(status1 >> ROM_VERSION_SHIFT) & ROM_VERSION_MASK,
        raw_cfg1=cfg1,
        raw_cfg2=cfg2,
        raw_status1=status1,
    )
