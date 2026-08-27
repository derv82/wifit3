"""RTL8188EUS channel tuning (20 MHz, 2.4 GHz only).

Cleanroom port of:

* `rtl8xxxu_read_rfreg`        — `core.c:867-905` (SIPI READ on path A)
* `rtl8188eu_config_channel`  — `8188e.c:423-522` (BW + channel + RF MODE_AG)

Restricted to the 20 MHz / 2.4 GHz subset: 8188e is 1T1R + 2.4 GHz only, so
neither path B nor 5 GHz nor 40 MHz code paths apply. The 40 MHz branch from
the kernel function is deliberately not ported.
"""
from __future__ import annotations

import logging
import time

from .constants import (
    BW_OPMODE_20MHZ,
    FPGA0_HSSI_PARM1_PI,
    FPGA0_HSSI_PARM2_ADDR_MASK,
    FPGA0_HSSI_PARM2_ADDR_SHIFT,
    FPGA0_HSSI_PARM2_EDGE_READ,
    FPGA_RF_MODE,
    MODE_AG_BW_20MHZ_8723B,
    MODE_AG_BW_MASK,
    MODE_AG_CHANNEL_MASK,
    REG_BW_OPMODE,
    REG_FPGA0_RF_MODE,
    REG_FPGA0_XA_HSSI_PARM1,
    REG_FPGA0_XA_HSSI_PARM2,
    REG_FPGA0_XA_LSSI_READBACK,
    REG_FPGA1_RF_MODE,
    REG_HSPI_XA_READBACK,
    RF6052_REG_MODE_AG,
    RF_READBACK_MASK,
)
from .phy import RF_A, write_rfreg
from .transport import RTL8188EUSTransport

logger = logging.getLogger(__name__)


def read_rfreg(t: RTL8188EUSTransport, path: int, reg: int) -> int:
    """Port of `rtl8xxxu_read_rfreg` (core.c:867-905).

    Issues a SIPI READ on path A, returning the lower 20 bits of the RF
    register value. The protocol:

    1. Snapshot REG_FPGA0_XA_HSSI_PARM2 (the path-A control register).
    2. Build a transaction value: replace ADDR field with `reg`, set
       EDGE_READ bit.
    3. Clear EDGE_READ in the path-A snapshot and write it back (puts
       the bus in idle for the read window).
    4. Wait 10 µs, write the transaction value to the path's PARM2.
    5. Wait 100 µs, set EDGE_READ in the path-A snapshot and write it
       back (latches the read result).
    6. Wait 10 µs, peek FPGA0_XA_HSSI_PARM1 — if `PI` bit set, read
       through HSPI readback register, else through LSSI readback.
    7. Mask result to lower 20 bits.

    8188e is 1T1R so the family `rtl8xxxu_rfregs[path].hssiparm2` lookup
    collapses to REG_FPGA0_XA_HSSI_PARM2 for the only RF path.
    """
    if path != RF_A:
        raise NotImplementedError("RTL8188EUS is 1T1R — only RF_A is supported")

    # 1. Snapshot path-A HSSI_PARM2.
    hssia = t.read32(REG_FPGA0_XA_HSSI_PARM2)
    # Same path, so val32 = hssia (the kernel `if path != RF_A` branch is dead here).
    val32 = hssia

    # 2. Build transaction value.
    val32 &= ~FPGA0_HSSI_PARM2_ADDR_MASK & 0xFFFFFFFF
    val32 |= (reg << FPGA0_HSSI_PARM2_ADDR_SHIFT) & FPGA0_HSSI_PARM2_ADDR_MASK
    val32 |= FPGA0_HSSI_PARM2_EDGE_READ

    # 3. Clear EDGE_READ in snapshot, write back to put the bus in idle.
    hssia &= ~FPGA0_HSSI_PARM2_EDGE_READ & 0xFFFFFFFF
    t.write32(REG_FPGA0_XA_HSSI_PARM2, hssia)
    time.sleep(0.000010)

    # 4. Write the transaction (same path-A register).
    t.write32(REG_FPGA0_XA_HSSI_PARM2, val32 & 0xFFFFFFFF)
    time.sleep(0.000100)

    # 5. Re-set EDGE_READ in snapshot, write back to latch.
    hssia |= FPGA0_HSSI_PARM2_EDGE_READ
    t.write32(REG_FPGA0_XA_HSSI_PARM2, hssia & 0xFFFFFFFF)
    time.sleep(0.000010)

    # 6. Peek HSSI_PARM1 to pick the right readback register.
    val32 = t.read32(REG_FPGA0_XA_HSSI_PARM1)
    if val32 & FPGA0_HSSI_PARM1_PI:
        retval = t.read32(REG_HSPI_XA_READBACK)
    else:
        retval = t.read32(REG_FPGA0_XA_LSSI_READBACK)

    # 7. Mask to 20 bits.
    return retval & RF_READBACK_MASK


def _replace_bits(value: int, new_field: int, mask: int) -> int:
    """Mirror of u32p_replace_bits — replace bits in `mask` of `value`
    with the corresponding bits of `new_field` (shifted to land in mask)."""
    shift = (mask & -mask).bit_length() - 1
    return (value & ~mask & 0xFFFFFFFF) | ((new_field << shift) & mask)


def set_channel_2g_20mhz(t: RTL8188EUSTransport, channel: int) -> None:
    """Port of `rtl8188eu_config_channel` 20 MHz path (8188e.c:431-447, 507-521).

    Tunes path A to the given 2.4 GHz channel (1-13) with 20 MHz width.
    Calls SIPI write twice on RF6052_REG_MODE_AG (0x18) — first to set
    the channel number, second to set the BW field. Each SIPI write
    issues one read (kernel uses `u32p_replace_bits` on a register read),
    one SIPI write.
    """
    if not 1 <= channel <= 14:
        raise ValueError(f"channel must be in 1..14 for 2.4 GHz; got {channel}")

    # --- 20 MHz BW path (kernel 8188e.c:436-448) ---
    opmode = t.read8(REG_BW_OPMODE)
    opmode |= BW_OPMODE_20MHZ
    t.write8(REG_BW_OPMODE, opmode)

    val32 = t.read32(REG_FPGA0_RF_MODE)
    val32 &= ~FPGA_RF_MODE & 0xFFFFFFFF
    t.write32(REG_FPGA0_RF_MODE, val32)

    val32 = t.read32(REG_FPGA1_RF_MODE)
    val32 &= ~FPGA_RF_MODE & 0xFFFFFFFF
    t.write32(REG_FPGA1_RF_MODE, val32)

    # --- RF MODE_AG: set channel (kernel 8188e.c:507-511) ---
    val32 = read_rfreg(t, RF_A, RF6052_REG_MODE_AG)
    val32 = _replace_bits(val32, channel, MODE_AG_CHANNEL_MASK)
    write_rfreg(t, RF_A, RF6052_REG_MODE_AG, val32)

    # --- RF MODE_AG: set BW=20 MHz (kernel 8188e.c:513-521) ---
    val32 = read_rfreg(t, RF_A, RF6052_REG_MODE_AG)
    val32 &= ~MODE_AG_BW_MASK & 0xFFFFFFFF
    val32 |= MODE_AG_BW_20MHZ_8723B
    write_rfreg(t, RF_A, RF6052_REG_MODE_AG, val32)

    logger.debug("tuned to channel %d @ 20 MHz", channel)
