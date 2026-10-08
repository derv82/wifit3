"""RTL8188FTV DKMS probe: chip-version read (M1).

Ported from ``rtl8188f_read_chip_version``
(hal/rtl8188f/rtl8188f_hal_init.c:2811-2867) + ``rtw_hal_config_rftype``
(hal/hal_com.c:98-125). One ``read32(REG_SYS_CFG)`` on the wire; the rest
(rf-type derivation, interface configure) is software.
"""
from __future__ import annotations

from dataclasses import dataclass


def BIT(n: int) -> int:
    return 1 << n


# include/hal_com_reg.h:104,1240,1249,1255
REG_SYS_CFG = 0x00F0
RTL_ID = BIT(23)
EXT_VENDOR_ID = BIT(18) | BIT(19)
EXT_VENDOR_ID_SHIFT = 18

# hal/phydm/rtl8188f/hal8188freg.h:638-639
CHIP_VER_RTL_MASK = 0xF000
CHIP_VER_RTL_SHIFT = 12

# include/HalVerDef.h:71-73 (2-bit field value -> vendor id)
VENDOR_TSMC = 0
VENDOR_UMC = 1
VENDOR_SMIC = 2

RF_TYPE_1T1R = 0


@dataclass
class ChipVersion:
    test_chip: bool
    vendor: int
    cut: int
    rf_paths: int


def read_chip_version(t) -> ChipVersion:
    value32 = t.read32(REG_SYS_CFG)
    vendor = VENDOR_TSMC
    tmpvdr = (value32 & EXT_VENDOR_ID) >> EXT_VENDOR_ID_SHIFT
    if tmpvdr == 0x00:
        vendor = VENDOR_TSMC
    elif tmpvdr == 0x01:
        vendor = VENDOR_SMIC
    elif tmpvdr == 0x02:
        vendor = VENDOR_UMC
    return ChipVersion(
        test_chip=bool(value32 & RTL_ID),
        vendor=vendor,
        cut=(value32 & CHIP_VER_RTL_MASK) >> CHIP_VER_RTL_SHIFT,
        rf_paths=1,
    )


def is_smic(version: ChipVersion) -> bool:
    return version.vendor == VENDOR_SMIC
