"""RTL8821CU chip-version read — SYS_CFG1 / SYS_STATUS1 / WL_BT_PWR_CTRL decode.

Ported 1:1 from [SRC] hal/rtl8821c/rtl8821c_ops.c:34 read_chip_version. The three reads
are what the cold-boot wire shows after the mount probe and before the EFUSE dump; the
decoded ``cut`` gates later init tables (read it at runtime — never hardcode this card's).

Bit fields [SRC] hal/halmac/halmac_bit_8821c.h: BIT_RTL_ID :2460, BIT_GET_CHIP_VER :2483,
BIT_GET_VENDOR_ID :2471, BIT_RF_TYPE_ID :2456, BIT_SPSLDO_SEL :2459, BIT_GET_RF_RL_ID :2510.
"""
from __future__ import annotations

from dataclasses import dataclass

REG_SYS_CFG1 = 0x00F0
REG_SYS_CFG2 = 0x00FC
REG_SYS_STATUS1 = 0x00F4
REG_WL_BT_PWR_CTRL = 0x0068

CHIP_ID_HW_DEF_8821C = 0x09     # [SRC] halmac_api.c:527 ; [WIRE] f534 SYS_CFG2 reads 0x09

_BIT_RTL_ID = 1 << 23           # set => TEST_CHIP, else NORMAL_CHIP
_BIT_RF_TYPE_ID = 1 << 27       # set => 2T2R, else 1T1R
_BIT_SPSLDO_SEL = 1 << 24       # set => LDO regulator, else switching
# halmac GET-macros are ((x >> shift) & mask):
_CHIP_VER_SHIFT, _CHIP_VER_MASK = 12, 0xF      # BIT_GET_CHIP_VER_8821C — the cut
_VENDOR_SHIFT, _VENDOR_MASK = 16, 0xF          # BIT_GET_VENDOR_ID_8821C
_RF_RL_ID_SHIFT, _RF_RL_ID_MASK = 28, 0xF      # BIT_GET_RF_RL_ID_8821C — ROM ver (from STATUS1)


@dataclass
class ChipVersion:
    raw_cfg1: int
    cut: int
    is_test_chip: bool
    vendor_raw: int     # >>2 in the vendor decode picks TSMC/SMIC/UMC; kept raw here
    rf_2t2r: bool
    ldo_regulator: bool
    rom_ver: int


def mount_get_chip_info(t) -> tuple[int, int]:
    """halmac mount-time chip detect, run before read_chip_version: chip_id from
    SYS_CFG2 (0xFC), chip_ver from SYS_CFG1+1 (0xF1) >> 4.
    [SRC] halmac_api.c:492 get_chip_info (USB path :518-520)."""
    chip_id = t.read8(REG_SYS_CFG2)
    chip_ver = t.read8(REG_SYS_CFG1 + 1) >> 4
    return chip_id, chip_ver


def read_chip_version(t) -> ChipVersion:
    """[SRC] rtl8821c_ops.c:42-72."""
    v = t.read32(REG_SYS_CFG1)
    cv = ChipVersion(
        raw_cfg1=v,
        cut=(v >> _CHIP_VER_SHIFT) & _CHIP_VER_MASK,
        is_test_chip=bool(v & _BIT_RTL_ID),
        vendor_raw=(v >> _VENDOR_SHIFT) & _VENDOR_MASK,
        rf_2t2r=bool(v & _BIT_RF_TYPE_ID),
        ldo_regulator=bool(v & _BIT_SPSLDO_SEL),
        rom_ver=0,
    )
    s = t.read32(REG_SYS_STATUS1)
    cv.rom_ver = (s >> _RF_RL_ID_SHIFT) & _RF_RL_ID_MASK
    t.read32(REG_WL_BT_PWR_CTRL)        # MultiFunc / PolarityCtl — decode unused so far
    return cv
