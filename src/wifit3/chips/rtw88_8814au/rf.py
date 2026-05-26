"""RTL8814AU RF read/write — 4-path (A/B/C/D), direct-read variant.

The 8814a's chip ops are `.read_rf = rtw_phy_read_rf` (generic, phy.c) and
`.write_rf = rtw_phy_write_rf_reg_sipi` (phy.c). This is the SAME pair the
8822b uses, and is DIFFERENT from the rtw88xxa 3-wire HSSI/PI/SI read that the
shared `rtw88_base/rf_sipi.py` implements for the 8812a/8821a. So 8814a gets
its own RF access here rather than reusing that shared helper.

  read  (rtw_phy_read_rf, phy.c):
      direct_addr = rf_base_addr[path] + (addr << 2)
      val = read32_mask(direct_addr, mask)     # plain MMIO read, no 3-wire

  write (rtw_phy_write_rf_reg_sipi, phy.c):
      if mask != RFREG_MASK: read-modify-merge via read_rf
      data_and_addr = ((addr << 20) | (data & 0xFFFFF)) & 0x0FFFFFFF
      write32(rf_sipi_addr[path], data_and_addr); udelay(13)

`path` is the RF-path index 0..3 (A/B/C/D), matching the kernel rtw_rf_path enum.
"""

from __future__ import annotations

import time

from .constants import RF_BASE_ADDR, RF_SIPI_ADDR
from .transport import RTL8814AUTransport

RFREG_MASK = 0xFFFFF          # phy.h:181
RF_PHY_NUM = 4                # 8814a is 4T4R


def _ffs(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def _check_path(path: int) -> None:
    if not 0 <= path < RF_PHY_NUM:
        raise ValueError(f"unsupported rf path {path} (0..{RF_PHY_NUM - 1})")


def read_rf(
    transport: RTL8814AUTransport,
    path: int,
    addr: int,
    mask: int = RFREG_MASK,
) -> int:
    """Direct MMIO read of RF reg `addr` on `path`, shifted into `mask`."""
    _check_path(path)
    addr &= 0xFF
    mask &= RFREG_MASK
    direct_addr = RF_BASE_ADDR[path] + (addr << 2)
    val = transport.read32(direct_addr)
    return (val & mask) >> _ffs(mask)


def write_rf(
    transport: RTL8814AUTransport,
    path: int,
    addr: int,
    mask: int,
    data: int,
    *,
    udelay_us: float = 13.0,
) -> None:
    """SIPI write to RF reg `addr` on `path` with mask-aware RMW."""
    _check_path(path)
    addr &= 0xFF
    mask &= RFREG_MASK

    if mask != RFREG_MASK:
        old = read_rf(transport, path, addr, RFREG_MASK)
        data = (old & ~mask) | ((data << _ffs(mask)) & mask)

    data_and_addr = ((addr << 20) | (data & RFREG_MASK)) & 0x0FFFFFFF
    transport.write32(RF_SIPI_ADDR[path], data_and_addr)
    if udelay_us:
        time.sleep(udelay_us / 1_000_000)
