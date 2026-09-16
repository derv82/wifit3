"""rtl8188ftv driver bring-up unit tests — chip_cut + vendor detection.

Locks the REG_SYS_CFG CHIP_VERSION extraction used by `_cold_bring_up`
(driver.py) to the kernel's bits 12-15 field (regs.h:342). A wrong-width
mask here silently picks the wrong RF table (`radio_a` vs `radio_a_cut_b`),
so the derivation itself is pinned to the captured wire value.
"""
from unittest.mock import MagicMock

from wifit3.chips.rtl8188ftv.constants import (
    REG_MACID,
    REG_SYS_CFG,
    SYS_CFG_CHIP_VERSION_MASK,
    SYS_CFG_TRP_VAUX_EN,
    SYS_CFG_VENDOR_EXT_MASK,
)
from wifit3.chips.rtl8188ftv.driver import RTL8188FTVDriver

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


class _Recorder:
    """Records REG_MACID byte writes for driver active-monitor tests."""

    def __init__(self):
        self.writes: list[tuple[int, int]] = []

    def write8(self, addr, val):
        self.writes.append((addr, val))

    def read8(self, addr):
        return 0


def _driver() -> RTL8188FTVDriver:
    d = RTL8188FTVDriver(MagicMock())
    d.transport = _Recorder()
    return d


async def test_enter_active_monitor_programs_macid_and_returns_mac():
    d = _driver()
    mac = bytes.fromhex("020000000001")
    assert await d.enter_active_monitor(mac) == mac
    assert [(a, v) for a, v in d.transport.writes] == [
        (REG_MACID + i, b) for i, b in enumerate(mac)
    ]


async def test_exit_active_monitor_restores_efuse_mac():
    d = _driver()
    d._mac_bytes = bytes.fromhex("aabbccddeeff")
    await d.enter_active_monitor(bytes.fromhex("020000000001"))
    d.transport.writes.clear()
    await d.exit_active_monitor()
    assert [(a, v) for a, v in d.transport.writes] == [
        (REG_MACID + i, b) for i, b in enumerate(d._mac_bytes)
    ]


async def test_exit_active_monitor_without_efuse_is_noop():
    d = _driver()
    d._mac_bytes = None
    await d.enter_active_monitor(bytes.fromhex("020000000001"))
    d.transport.writes.clear()
    await d.exit_active_monitor()
    assert d.transport.writes == []