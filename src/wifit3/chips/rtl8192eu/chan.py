"""RTL8192EUS channel tuning (2.4 GHz, 20 MHz).

Ports the shared SIPI read (`rtl8xxxu_read_rfreg`, core.c:867-905) and the
8192eu channel routine `rtl8xxxu_gen2_config_channel` (core.c:1328-1458,
wired as `.config_channel` at 8192e.c:1734). Only the 20 MHz / 2.4 GHz
branch is ported; 40/80 MHz need chandef fields the monitor path never sets.
"""
from __future__ import annotations

import logging
import time

from .constants import (
    FPGA0_HSSI_PARM1_PI,
    FPGA0_HSSI_PARM2_ADDR_MASK,
    FPGA0_HSSI_PARM2_ADDR_SHIFT,
    FPGA0_HSSI_PARM2_EDGE_READ,
    FPGA_RF_MODE,
    MODE_AG_BW_20MHZ_8723B,
    MODE_AG_BW_MASK,
    MODE_AG_CHANNEL_MASK,
    REG_FPGA0_RF_MODE,
    REG_FPGA0_XA_HSSI_PARM2,
    REG_FPGA1_RF_MODE,
    REG_OFDM0_TX_PSDO_NOISE_WEIGHT,
    REG_SIFS_CCK,
    REG_SIFS_OFDM,
    REG_WMAC_TRXPTCL_CTL,
    RF6052_REG_MODE_AG,
    RF_READBACK_MASK,
    WMAC_TRXPTCL_CTL_BW_20,
    WMAC_TRXPTCL_CTL_BW_MASK,
)
from .phy import RF_A, RF_B, RF_REGS, write_rfreg
from .transport import RTL8192EUTransport

# Used by gen2_config_channel but not yet exported by constants.py.
REG_DATA_SUBCHANNEL = 0x0483    # regs.h:614
REG_R2T_SIFS = 0x063c           # regs.h:799
REG_T2T_SIFS = 0x063e           # regs.h:801

logger = logging.getLogger(__name__)


def read_rfreg(t: RTL8192EUTransport, path: int, reg: int) -> int:
    """Port of `rtl8xxxu_read_rfreg` (core.c:867-905), both RF paths.

    1. Snapshot REG_FPGA0_XA_HSSI_PARM2 (the path-A control register).
    2. Build a transaction value: replace ADDR with `reg`, set EDGE_READ.
       For path B the transaction goes to REG_FPGA0_XB_HSSI_PARM2
       (core.c:873-876), which is also read to seed the value.
    3. Clear EDGE_READ in the path-A snapshot and write it back (bus idle).
    4. Wait 10 µs, write the transaction value to the path's PARM2.
    5. Wait 100 µs, set EDGE_READ in the snapshot and write it back (latch).
    6. Wait 10 µs, peek the path's HSSI_PARM1 — if `PI` set, read through the
       path's HSPI readback, else through its LSSI readback (core.c:893-897).
    7. Mask the result to the lower 20 bits.
    """
    regs = RF_REGS.get(path)
    if regs is None:
        raise ValueError(f"unsupported RF path {path!r} (expected RF_A or RF_B)")

    # 1. Snapshot path-A HSSI_PARM2 (val32 = hssia for RF_A; core.c:872-876).
    hssia = t.read32(REG_FPGA0_XA_HSSI_PARM2)
    if path != RF_A:
        val32 = t.read32(regs.hssi_parm2)
    else:
        val32 = hssia

    # 2. Build the transaction value (core.c:878-880).
    val32 &= ~FPGA0_HSSI_PARM2_ADDR_MASK & 0xFFFFFFFF
    val32 |= (reg << FPGA0_HSSI_PARM2_ADDR_SHIFT) & FPGA0_HSSI_PARM2_ADDR_MASK
    val32 |= FPGA0_HSSI_PARM2_EDGE_READ

    # 3. Clear EDGE_READ and write the path-A snapshot back (core.c:881-884).
    hssia &= ~FPGA0_HSSI_PARM2_EDGE_READ & 0xFFFFFFFF
    t.write32(REG_FPGA0_XA_HSSI_PARM2, hssia)
    time.sleep(0.000010)

    # 4. Write the transaction to the path's PARM2 (core.c:886-887).
    t.write32(regs.hssi_parm2, val32 & 0xFFFFFFFF)
    time.sleep(0.000100)

    # 5. Re-set EDGE_READ and write the path-A snapshot back (core.c:889-891).
    hssia |= FPGA0_HSSI_PARM2_EDGE_READ
    t.write32(REG_FPGA0_XA_HSSI_PARM2, hssia & 0xFFFFFFFF)
    time.sleep(0.000010)

    # 6. Peek the path's HSSI_PARM1 to pick the readback register (core.c:893-897).
    val32 = t.read32(regs.hssi_parm1)
    if val32 & FPGA0_HSSI_PARM1_PI:
        retval = t.read32(regs.hspi_readback)
    else:
        retval = t.read32(regs.lssi_readback)

    # 7. Mask to the lower 20 bits (core.c:899).
    return retval & RF_READBACK_MASK


def set_channel_2g_20mhz(t: RTL8192EUTransport, channel: int) -> None:
    """Tune both RF paths to a 2.4 GHz channel at 20 MHz.

    Port of the 20 MHz half of `rtl8xxxu_gen2_config_channel`
    (core.c:1338-1457). 8192eu is 2T2R (`rf_paths = 2`, `8192e.c:490`), so
    both kernel loops — channel field (core.c:1421-1426) and BW field
    (core.c:1442-1457) — run for RF_A and RF_B.
    """
    if not 1 <= channel <= 14:
        raise ValueError(f"channel must be in 1..14 for 2.4 GHz; got {channel}")

    # core.c:1338-1339 — clear the 16-bit BW field of WMAC_TRXPTCL_CTL.
    rf_mode_bw = t.read16(REG_WMAC_TRXPTCL_CTL)
    rf_mode_bw &= ~WMAC_TRXPTCL_CTL_BW_MASK & 0xFFFF

    # core.c:1349-1365 — NL80211_CHAN_WIDTH_20 (HT). The 20_NOHT width
    # (core.c:1346-1348) differs only in the SIFS byte written below.
    ht = True
    rf_mode_bw |= WMAC_TRXPTCL_CTL_BW_20
    subchannel = 0

    val32 = t.read32(REG_FPGA0_RF_MODE)
    val32 &= ~FPGA_RF_MODE & 0xFFFFFFFF
    t.write32(REG_FPGA0_RF_MODE, val32)

    val32 = t.read32(REG_FPGA1_RF_MODE)
    val32 &= ~FPGA_RF_MODE & 0xFFFFFFFF
    t.write32(REG_FPGA1_RF_MODE, val32)

    # core.c:1361-1363 — clear BIT(30) | BIT(31).
    val32 = t.read32(REG_OFDM0_TX_PSDO_NOISE_WEIGHT)
    val32 &= ~((1 << 30) | (1 << 31)) & 0xFFFFFFFF
    t.write32(REG_OFDM0_TX_PSDO_NOISE_WEIGHT, val32)

    # core.c:1421-1426 — RF MODE_AG channel field, both paths.
    for path in (RF_A, RF_B):
        val32 = read_rfreg(t, path, RF6052_REG_MODE_AG)
        val32 &= ~MODE_AG_CHANNEL_MASK & 0xFFFFFFFF
        val32 |= channel
        write_rfreg(t, path, RF6052_REG_MODE_AG, val32)

    # core.c:1428-1429
    t.write16(REG_WMAC_TRXPTCL_CTL, rf_mode_bw)
    t.write8(REG_DATA_SUBCHANNEL, subchannel)

    # core.c:1431-1437 — SIFS_CCK/OFDM high byte: 0x0e when ht else 0x0a.
    val8 = 0x0e if ht else 0x0a
    t.write8(REG_SIFS_CCK + 1, val8)
    t.write8(REG_SIFS_OFDM + 1, val8)

    # core.c:1439-1440
    t.write16(REG_R2T_SIFS, 0x0808)
    t.write16(REG_T2T_SIFS, 0x0a0a)

    # core.c:1442-1457 — RF MODE_AG BW field = MODE_AG_BW_20MHZ_8723B, both paths.
    for path in (RF_A, RF_B):
        val32 = read_rfreg(t, path, RF6052_REG_MODE_AG)
        val32 &= ~MODE_AG_BW_MASK & 0xFFFFFFFF
        val32 |= MODE_AG_BW_20MHZ_8723B
        write_rfreg(t, path, RF6052_REG_MODE_AG, val32)

    logger.debug("tuned to channel %d @ 20 MHz", channel)
