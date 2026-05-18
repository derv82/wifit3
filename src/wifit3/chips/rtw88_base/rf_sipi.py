"""RF SIPI read / write primitives, shared across the rtw88 family.

Ports `rtw_phy_write_rf_reg_sipi` (phy.c:1029) for writes and
`rtw88xxa_phy_read_rf` (rtw88xxa.c:1245) for reads. The procedure is the
same on every rtw88 USB chip; only the per-chip RF register address space
and timing varies.

These helpers are PATH-PARAMETERISED: pass `lssi_write` / `hssi_read` /
`pi_read` / `si_read` for the path you want (A or B). The 3-wire status
bit lives in REG_3WIRE_SWA for both paths on USB-RF chips.
"""

from __future__ import annotations

import time

from .transport import Rtw88Transport

# Register addresses (phy.c & rtw88xxa.c) — shared across chips.
REG_3WIRE_SWA = 0x0C00
REG_HSSI_READ = 0x08B0
REG_LSSI_WRITE_A = 0x0C90
REG_LSSI_WRITE_B = 0x0E90
REG_PI_READ_A = 0x0D04
REG_SI_READ_A = 0x0D08
REG_PI_READ_B = 0x0D44
REG_SI_READ_B = 0x0D48

# RF register mask (phy.h:181).
RFREG_MASK = 0xFFFFF


def _ffs(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def read_rf(
    transport: Rtw88Transport,
    addr: int,
    mask: int = RFREG_MASK,
    *,
    path: str = "a",
    udelay_us: float = 20.0,
) -> int:
    """SIPI read of RF reg `addr` shifted into `mask`'s position.

    Path 'a' → PI/SI_READ_A; path 'b' → PI/SI_READ_B.
    """
    if path == "a":
        pi_read, si_read = REG_PI_READ_A, REG_SI_READ_A
    elif path == "b":
        pi_read, si_read = REG_PI_READ_B, REG_SI_READ_B
    else:
        raise ValueError(f"path must be 'a' or 'b', got {path!r}")

    addr &= 0xFF
    pi_mode = (transport.read32(REG_3WIRE_SWA) >> 2) & 1   # BIT(2)
    transport.write32_mask(REG_HSSI_READ, 0xFF, addr)
    if udelay_us:
        time.sleep(udelay_us / 1_000_000)
    cur = transport.read32(pi_read if pi_mode else si_read)
    shift = _ffs(mask)
    return (cur & mask) >> shift


def write_rf_masked(
    transport: Rtw88Transport,
    addr: int,
    mask: int,
    data: int,
    *,
    path: str = "a",
    udelay_us: float = 13.0,
) -> None:
    """SIPI write to RF reg `addr` with mask-aware RMW.

    Mirrors `rtw_phy_write_rf_reg_sipi` (phy.c:1029). If `mask == RFREG_MASK`
    the kernel skips the read-back; otherwise it reads, merges, writes.
    """
    if path == "a":
        lssi_write = REG_LSSI_WRITE_A
    elif path == "b":
        lssi_write = REG_LSSI_WRITE_B
    else:
        raise ValueError(f"path must be 'a' or 'b', got {path!r}")

    addr &= 0xFF
    mask &= RFREG_MASK

    if mask != RFREG_MASK:
        old = read_rf(transport, addr, RFREG_MASK, path=path, udelay_us=udelay_us)
        shift = _ffs(mask)
        data = (old & ~mask) | ((data << shift) & mask)

    data_and_addr = ((addr << 20) | (data & RFREG_MASK)) & 0x0FFFFFFF
    transport.write32(lssi_write, data_and_addr)
    if udelay_us:
        time.sleep(udelay_us / 1_000_000)
