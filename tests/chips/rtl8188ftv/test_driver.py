"""rtl8188ftv driver bring-up unit tests — chip_cut + vendor detection.

Locks the REG_SYS_CFG CHIP_VERSION extraction used by `_cold_bring_up`
(driver.py) to the kernel's bits 12-15 field (regs.h:342). A wrong-width
mask here silently picks the wrong RF table (`radio_a` vs `radio_a_cut_b`),
so the derivation itself is pinned to the captured wire value.
"""
from wifit3.chips.rtl8188ftv.constants import (
    REG_SYS_CFG,
    SYS_CFG_CHIP_VERSION_MASK,
    SYS_CFG_TRP_VAUX_EN,
    SYS_CFG_VENDOR_EXT_MASK,
)

# The real REG_SYS_CFG captured at cold boot (dev 8, capture-1).
# Kernel derives chip_cut = u32_get_bits(sys_cfg, 0xf000) == 1 (cut B).
CAPTURED_SYS_CFG = 0x04441525


def test_chip_version_mask_is_bits_12to15():
    assert SYS_CFG_CHIP_VERSION_MASK == 0xF000


def test_chip_cut_derivation_matches_capture():
    cut = (CAPTURED_SYS_CFG & SYS_CFG_CHIP_VERSION_MASK) >> 12
    assert cut == 1
    assert cut == (CAPTURED_SYS_CFG >> 12) & 0xF


def test_vendor_ext_mask_and_trp_bit():
    vendor = CAPTURED_SYS_CFG & SYS_CFG_VENDOR_EXT_MASK
    assert vendor == 0x0004_0000
    assert not (CAPTURED_SYS_CFG & SYS_CFG_TRP_VAUX_EN)


def test_reg_sys_cfg_constant_matches_wire():
    assert REG_SYS_CFG == 0x00F0