"""RTL8192EU PHY init — BB + AGC + both RF paths (2T2R).

Cleanroom port of:

* `rtl8xxxu_init_phy_regs`   — `core.c:2228-2252` (BB/AGC table loader)
* `rtl8xxxu_write_rfreg`     — `core.c:912-947` (SIPI 20-bit encode → LSSI_PARM,
  plus the RTL8192E-only `REG_FPGA0_POWER_SAVE` BIT(17) bracket)
* `rtl8xxxu_init_rf_regs`    — `core.c:2385-2431` (per-entry SIPI write w/ delay opcodes)
* `rtl8xxxu_init_phy_rf`     — `core.c:2433-2496` (per-path SW_CTRL wrap + RF table)
* `rtl8192eu_init_phy_bb`    — `8192e.c:652-677` (BB prep, AGC table by `hi_pa`)
* `rtl8192eu_init_phy_rf`    — `8192e.c:679-691` (radio A + radio B tables)
* `rtl8xxxu_init_phy_bb`     — `core.c:2310-2383` (set_crystal_cap + 8192E XTAL write)
* `rtl8723a_set_crystal_cap` — `8723a.c:530-554` (wired at `8192e.c:1745`)
* `rtl8192e_enable_rf`       — `8192e.c:1629-1673` (wired at `8192e.c:1737`)
* `rtl8xxxu_gen2_disable_rf` — `core.c:3806-3813`
* `rtl8xxxu_gen2_usb_quirks` — `core.c:3782-3789`
* RFSW control block         — `core.c:4007-4014`
* thermal-meter enable       — `core.c:4297-4305`
* LDPC-rx-hang fix           — `core.c:4334-4342`
* CCK AGC report alignment   — `core.c:4348-4367`
* `rtl8xxxu_gen2_channel_to_group` — `core.c:1188-1204`
* `rtl8192e_set_tx_power`    — `8192e.c:512-586`

Calibration lives in `iqk.py` (`.phy_iq_calibrate` / `.phy_lc_calibrate`); the CCK
RSSI lookup is in `rx.py` (`.cck_rssi`).
"""
from __future__ import annotations

import logging
import time
from typing import NamedTuple

from .constants import (
    AGC_RPT_CCK,
    FPGA0_HSSI_3WIRE_ADDR_LEN,
    FPGA0_HSSI_3WIRE_DATA_LEN,
    FPGA0_HSSI_PARM2_CCK_HIGH_PWR,
    FPGA0_LSSI_PARM_ADDR_SHIFT,
    FPGA0_LSSI_PARM_DATA_MASK,
    FPGA0_RF_ANTSW,
    FPGA0_RF_ANTSWB,
    FPGA0_RF_BD_CTRL_SHIFT,
    FPGA0_RF_PAPE,
    FPGA0_RF_RFENV,
    FPGA0_RF_TRSW,
    FPGA0_RF_TRSWB,
    FPGA_RF_MODE_CCK,
    FPGA_RF_MODE_OFDM,
    PWR_DATA_EEPRPAD_RFE_CTRL_EN,
    REG_8192E_LDOV12_CTRL,
    REG_AFE_CTRL4,
    REG_AFE_MISC,
    REG_AFE_PLL_CTRL,
    REG_AFE_XTAL_CTRL,
    REG_AGC_RPT,
    REG_FPGA0_RF_MODE,
    REG_FPGA0_XA_HSSI_PARM1,
    REG_FPGA0_XA_HSSI_PARM2,
    REG_FPGA0_XA_LSSI_PARM,
    REG_FPGA0_XA_LSSI_READBACK,
    REG_FPGA0_XA_RF_INT_OE,
    REG_FPGA0_XA_RF_SW_CTRL,
    REG_FPGA0_XAB_RF_SW_CTRL,
    REG_FPGA0_XB_HSSI_PARM1,
    REG_FPGA0_XB_HSSI_PARM2,
    REG_FPGA0_XB_LSSI_PARM,
    REG_FPGA0_XB_LSSI_READBACK,
    REG_FPGA0_XB_RF_INT_OE,
    REG_FPGA0_XB_RF_SW_CTRL,
    REG_GPIO_MUXCFG,
    REG_HSPI_XA_READBACK,
    REG_HSPI_XB_READBACK,
    REG_LEDCFG0,
    REG_PAD_CTRL1,
    REG_PWR_DATA,
    REG_RF_CTRL,
    REG_RFE_BUFFER,
    REG_RFE_CTRL_ANTA_SRC,
    REG_RX_WAIT_CCA,
    REG_SYS_FUNC,
    REG_TX_AGC_A_CCK1_MCS32,
    REG_TX_AGC_A_MCS03_MCS00,
    REG_TX_AGC_A_MCS07_MCS04,
    REG_TX_AGC_A_MCS11_MCS08,
    REG_TX_AGC_A_MCS15_MCS12,
    REG_TX_AGC_A_RATE18_06,
    REG_TX_AGC_A_RATE54_24,
    REG_TX_AGC_B_CCK1_55_MCS32,
    REG_TX_AGC_B_CCK11_A_CCK2_11,
    REG_TX_AGC_B_MCS03_MCS00,
    REG_TX_AGC_B_MCS07_MCS04,
    REG_TX_AGC_B_MCS11_MCS08,
    REG_TX_AGC_B_MCS15_MCS12,
    REG_TX_AGC_B_RATE18_06,
    REG_TX_AGC_B_RATE54_24,
    REG_TXDMA_OFFSET_CHK,
    REG_TXPAUSE,
    REG_WLAN_ACT_CONTROL_8723B,
    RF6052_REG_T_METER_8723B,
    RF_ENABLE,
    RF_RSTB,
    RF_SDMRSTB,
    SYS_FUNC_BB_GLB_RSTN,
    SYS_FUNC_BBRSTB,
    SYS_FUNC_DIO_RF,
    SYS_FUNC_USBA,
    SYS_FUNC_USBD,
    TXDMA_OFFSET_DROP_DATA_EN,
)
from .efuse import EfuseDefaults
from .phy_tables import (
    AGC_HIGHP_A_TABLE_8192E,
    AGC_STD_TABLE_8192E,
    PHY_INIT_TABLE_8192E,
    RADIO_A_INIT_TABLE_8192E,
    RADIO_B_INIT_TABLE_8192E,
)
from .transport import RTL8192EUTransport

logger = logging.getLogger(__name__)

# RF path enum — mirrors `enum rtl8xxxu_rfpath` in rtl8xxxu.h. 8192eu is 2T2R.
RF_A = 0
RF_B = 1

# Not exported by constants.py; cited from regs.h.
REG_FPGA0_POWER_SAVE = 0x0818     # regs.h:899
FPGA0_POWER_SAVE_BIT17 = 1 << 17  # 0x20000, core.c:927 / 942
REG_MAC_PHY_CTRL = 0x002C         # regs.h:119

# `rtl8723a_set_crystal_cap` trims REG_MAC_PHY_CTRL, not REG_AFE_XTAL_CTRL
# (the 8188f layout constants.py exports). GENMASK literals from 8723a.c:527-528.
XTAL1 = 0x00FC0000                # GENMASK(23, 18), 8723a.c:527
XTAL0 = 0x0003F000                # GENMASK(17, 12), 8723a.c:528
XTAL1_SHIFT = 18
XTAL0_SHIFT = 12

# `rtl8xxxu_init_phy_bb` writes this literal on RTL8192E only (core.c:2379-2380).
AFE_XTAL_CTRL_8192E_INIT = 0x000F81FB


class _RfRegs(NamedTuple):
    """Per-path register set (`struct rtl8xxxu_rfregs`, core.c:580-597).

    `int_oe` is not a struct field but the per-path INT_OE register
    `rtl8xxxu_init_phy_rf` pokes (core.c:2452, 2456).
    """

    sw_ctrl: int
    int_oe: int
    hssi_parm1: int
    hssi_parm2: int
    lssi_parm: int
    hspi_readback: int
    lssi_readback: int


# `rtl8xxxu_rfregs[]` (core.c:580-597). Shared by `write_rfreg` here and the
# SIPI read in `chan.read_rfreg` (core.c:867-905 reads both paths).
RF_REGS = {
    RF_A: _RfRegs(REG_FPGA0_XA_RF_SW_CTRL, REG_FPGA0_XA_RF_INT_OE,
                  REG_FPGA0_XA_HSSI_PARM1, REG_FPGA0_XA_HSSI_PARM2,
                  REG_FPGA0_XA_LSSI_PARM, REG_HSPI_XA_READBACK,
                  REG_FPGA0_XA_LSSI_READBACK),
    RF_B: _RfRegs(REG_FPGA0_XB_RF_SW_CTRL, REG_FPGA0_XB_RF_INT_OE,
                  REG_FPGA0_XB_HSSI_PARM1, REG_FPGA0_XB_HSSI_PARM2,
                  REG_FPGA0_XB_LSSI_PARM, REG_HSPI_XB_READBACK,
                  REG_FPGA0_XB_LSSI_READBACK),
}


def init_phy_regs(t: RTL8192EUTransport, table) -> int:
    """Port of `rtl8xxxu_init_phy_regs` (core.c:2228-2252).

    Iterates `table` (sequence of `(u16 reg, u32 val)` pairs) writing each
    via write32. Sleeps 1 µs between writes per the kernel.
    """
    count = 0
    for reg, val in table:
        t.write32(reg, val & 0xFFFFFFFF)
        time.sleep(0.000001)
        count += 1
    return count


def write_rfreg(t: RTL8192EUTransport, path: int, reg: int, data: int) -> None:
    """Port of `rtl8xxxu_write_rfreg` (core.c:912-947).

    Encodes ``(reg << 20) | (data & 0x000FFFFF)`` and writes it to the
    LSSI parameter register for `path` (XA for path A, XB for path B —
    core.c:932 + rtl8xxxu_rfregs[]). On RTL8192E the kernel clears
    `REG_FPGA0_POWER_SAVE` BIT(17) before the write and restores it after
    (core.c:925-929, 940-944) — the write silently fails otherwise.
    """
    regs = RF_REGS.get(path)
    if regs is None:
        raise ValueError(f"unsupported RF path {path!r} (expected RF_A or RF_B)")

    dataaddr = (reg << FPGA0_LSSI_PARM_ADDR_SHIFT) | (data & FPGA0_LSSI_PARM_DATA_MASK)

    val32 = t.read32(REG_FPGA0_POWER_SAVE)
    val32 &= ~FPGA0_POWER_SAVE_BIT17 & 0xFFFFFFFF
    t.write32(REG_FPGA0_POWER_SAVE, val32)

    t.write32(regs.lssi_parm, dataaddr & 0xFFFFFFFF)
    time.sleep(0.000001)

    val32 = t.read32(REG_FPGA0_POWER_SAVE)
    val32 |= FPGA0_POWER_SAVE_BIT17
    t.write32(REG_FPGA0_POWER_SAVE, val32 & 0xFFFFFFFF)


# Delay opcodes used in RF init tables (`core.c:2400-2418`). When the
# 'register address' equals one of these magic values, the kernel sleeps
# instead of issuing a SIPI write.
_RF_DELAY_OPCODES = {
    0xFE: 0.05,        # msleep(50)
    0xFD: 0.005,       # mdelay(5)
    0xFC: 0.001,       # mdelay(1)
    0xFB: 0.00005,     # udelay(50)
    0xFA: 0.000005,    # udelay(5)
    0xF9: 0.000001,    # udelay(1)
}


def init_rf_regs(t: RTL8192EUTransport, table, path: int = RF_A) -> int:
    """Port of `rtl8xxxu_init_rf_regs` (core.c:2385-2431).

    Iterates `table` (sequence of `(u8 reg, u32 val)` pairs). Magic
    register addresses 0xF9..0xFE are timing opcodes (see kernel
    `core.c:2400-2418`); everything else is a SIPI write.
    """
    count = 0
    for reg, val in table:
        delay = _RF_DELAY_OPCODES.get(reg)
        if delay is not None:
            time.sleep(delay)
            continue
        write_rfreg(t, path, reg, val)
        count += 1
    return count


def init_phy_rf(t: RTL8192EUTransport, table, path: int = RF_A) -> None:
    """Port of `rtl8xxxu_init_phy_rf` (core.c:2433-2496).

    Saves RFENV, sets up this path's SW_CTRL / INT_OE / HSSI_PARM2 for SIPI
    access, runs the RF table, then restores RFENV. 8192eu runs both A and B.
    """
    regs = RF_REGS.get(path)
    if regs is None:
        raise ValueError(f"unsupported RF path {path!r} (expected RF_A or RF_B)")

    # Save RFENV.
    rfsi_rfenv = t.read16(regs.sw_ctrl) & FPGA0_RF_RFENV

    # SIPI access setup — two BIT-set pokes to INT_OE, then two BIT-clear
    # pokes to HSSI_PARM2 (3-wire serial mode).
    val32 = t.read32(regs.int_oe) | (1 << 20)
    t.write32(regs.int_oe, val32 & 0xFFFFFFFF)
    time.sleep(0.000001)

    val32 = t.read32(regs.int_oe) | (1 << 4)
    t.write32(regs.int_oe, val32 & 0xFFFFFFFF)
    time.sleep(0.000001)

    val32 = t.read32(regs.hssi_parm2) & ~FPGA0_HSSI_3WIRE_ADDR_LEN
    t.write32(regs.hssi_parm2, val32 & 0xFFFFFFFF)
    time.sleep(0.000001)

    val32 = t.read32(regs.hssi_parm2) & ~FPGA0_HSSI_3WIRE_DATA_LEN
    t.write32(regs.hssi_parm2, val32 & 0xFFFFFFFF)
    time.sleep(0.000001)

    # The RF init table itself.
    init_rf_regs(t, table, path)

    # Restore RFENV.
    val16 = t.read16(regs.sw_ctrl)
    val16 = (val16 & ~FPGA0_RF_RFENV & 0xFFFF) | rfsi_rfenv
    t.write16(regs.sw_ctrl, val16)


def init_phy_bb(t: RTL8192EUTransport, *, hi_pa: bool = False) -> None:
    """Port of `rtl8192eu_init_phy_bb` (8192e.c:652-677).

    BB prep pokes, `PHY_INIT_TABLE_8192E`, then the AGC table — the high-PA
    variant when `priv->hi_pa` is set (8192e.c:673-676). 8192eu never sets
    `hi_pa` (`8192c.c:459` is the only assignment), so the std table is used.
    """
    val16 = t.read16(REG_SYS_FUNC)
    val16 |= SYS_FUNC_BB_GLB_RSTN | SYS_FUNC_BBRSTB | SYS_FUNC_DIO_RF
    t.write16(REG_SYS_FUNC, val16)

    # 6. 0x1f[7:0] = 0x07
    t.write8(REG_RF_CTRL, RF_ENABLE | RF_RSTB | RF_SDMRSTB)

    val16 = t.read16(REG_SYS_FUNC)
    val16 |= (SYS_FUNC_USBA | SYS_FUNC_USBD | SYS_FUNC_DIO_RF |
              SYS_FUNC_BB_GLB_RSTN | SYS_FUNC_BBRSTB)
    t.write16(REG_SYS_FUNC, val16)
    t.write8(REG_RF_CTRL, RF_ENABLE | RF_RSTB | RF_SDMRSTB)

    logger.debug("loading PHY_INIT_TABLE_8192E (%d entries)", len(PHY_INIT_TABLE_8192E))
    init_phy_regs(t, PHY_INIT_TABLE_8192E)

    agc_table = AGC_HIGHP_A_TABLE_8192E if hi_pa else AGC_STD_TABLE_8192E
    logger.debug("loading %s AGC table (%d entries)",
                 "high-PA" if hi_pa else "std", len(agc_table))
    init_phy_regs(t, agc_table)


def init_phy_rf_8192e(t: RTL8192EUTransport) -> None:
    """Port of `rtl8192eu_init_phy_rf` (8192e.c:679-691): radio A then radio B."""
    logger.debug("loading RADIO_A_INIT_TABLE_8192E (%d entries)", len(RADIO_A_INIT_TABLE_8192E))
    init_phy_rf(t, RADIO_A_INIT_TABLE_8192E, RF_A)

    logger.debug("loading RADIO_B_INIT_TABLE_8192E (%d entries)", len(RADIO_B_INIT_TABLE_8192E))
    init_phy_rf(t, RADIO_B_INIT_TABLE_8192E, RF_B)


def set_crystal_cap(t: RTL8192EUTransport, crystal_cap: int, prev_cap: int = 0) -> None:
    """Port of `rtl8723a_set_crystal_cap` (8723a.c:530-554).

    Writes the 6-bit EFUSE crystal-cap trim into both XTAL1 and XTAL0 of
    `REG_MAC_PHY_CTRL`. The kernel skips the write when the cap already equals
    the cached value (`cfo->crystal_cap`); at cold bring-up that cache is 0, so a
    non-zero EFUSE cap always writes. `crystal_cap == 0` (no EFUSE / unparsed) is
    treated as "leave the hardware default" — the same skip the kernel takes.
    """
    if crystal_cap == prev_cap:
        return
    val32 = t.read32(REG_MAC_PHY_CTRL)
    val32 &= ~(XTAL1 | XTAL0) & 0xFFFFFFFF
    val32 |= ((crystal_cap << XTAL1_SHIFT) & XTAL1) | ((crystal_cap << XTAL0_SHIFT) & XTAL0)
    t.write32(REG_MAC_PHY_CTRL, val32 & 0xFFFFFFFF)


def crystal_afe_adjust(t: RTL8192EUTransport) -> None:
    """Port of `rtl8192e_crystal_afe_adjust` (8192e.c:1330-1360).

    AFE/PLL preselection run by `rtl8192eu_power_on` (8192e.c:1570) before the
    PLL is enabled: 40 MHz crystal source (0x28[2]=0), KVCO selection (0x28[6]=0),
    and the AFE_CTRL4 clears at 0x78[9:7] and 0x78[21].
    """
    # 40Mhz crystal source, MAC 0x28[2]=0.
    val8 = t.read8(REG_AFE_PLL_CTRL)
    val8 &= ~(1 << 2) & 0xFF
    t.write8(REG_AFE_PLL_CTRL, val8)

    val32 = t.read32(REG_AFE_CTRL4)
    val32 &= 0xFFFFFC7F
    t.write32(REG_AFE_CTRL4, val32)

    # AFE PLL KVCO selection — 0x28[6] cleared (0xbf mask, 8192e.c:1351).
    val8 = t.read8(REG_AFE_PLL_CTRL)
    val8 &= ~(1 << 6) & 0xFF
    t.write8(REG_AFE_PLL_CTRL, val8)

    # AFE PLL KVCO selection, MAC 0x78[21]=0.
    val32 = t.read32(REG_AFE_CTRL4)
    val32 &= 0xFFDFFFFF
    t.write32(REG_AFE_CTRL4, val32)


def post_mac_init_phy(
    t: RTL8192EUTransport,
    efuse: EfuseDefaults,
    *,
    hi_pa: bool = False,
) -> None:
    """Run the PHY init sequence: BB + AGC + crystal cap + both RF paths.

    Mirrors `rtl8xxxu_init_phy_bb` (core.c:2310-2383) followed by
    `fops->init_phy_rf` (core.c:3988-3995). The 1T2R patch block
    (core.c:2316-2374) does not apply: 8192eu is 2T2R.
    """
    init_phy_bb(t, hi_pa=hi_pa)
    set_crystal_cap(t, efuse.default_crystal_cap)
    t.write32(REG_AFE_XTAL_CTRL, AFE_XTAL_CTRL_8192E_INIT)  # core.c:2379-2380, 8192E only
    init_phy_rf_8192e(t)


# ---- TX power -----------------------------------------------------


def channel_to_group_8192e(channel: int) -> int:
    """Port of `rtl8xxxu_gen2_channel_to_group` (core.c:1188-1204).

    Maps a 2.4 GHz channel (1-14) to a TX-power group index; the same
    `priv->cck_tx_power_index_*[group]` / `ht40_1s_tx_power_index_*[group]`
    index serves both CCK and OFDM (8192e.c:522, 534).

        ch 1-2   → 0
        ch 3-5   → 1
        ch 6-8   → 2
        ch 9-11  → 3
        ch 12-14 → 4
    """
    if channel < 3:
        return 0
    if channel < 6:
        return 1
    if channel < 9:
        return 2
    if channel < 12:
        return 3
    return 4


def _pack4(byte_val: int) -> int:
    """Replicate `byte_val` across 4 byte lanes of a u32 (chip's standard
    "same power for all 4 rates in this register" pattern)."""
    b = byte_val & 0xFF
    return b | (b << 8) | (b << 16) | (b << 24)


def set_tx_power(
    t: RTL8192EUTransport,
    channel: int,
    efuse: EfuseDefaults,
    *,
    ht40: bool = False,
) -> None:
    """Port of `rtl8192e_set_tx_power` (8192e.c:512-586).

    Writes per-channel-group CCK + OFDM + MCS power indices for both RF paths
    to the TX AGC registers based on EFUSE values. `priv->tx_paths > 1` gates
    the path-B half; 8192EU has `tx_paths == 2` (8192e.c:483-486 identifies
    8191EU as the 1-path variant), and the 8191EU half is not ported.

    The kernel's shared `tx_idx` starts at 0 for path A and is post-incremented
    by path A's MCS diff lookup, so path B reads index 1 of every per-path diff
    table (8192e.c:519, 535, 543-545, 567, 576-578).

    Without this call the TX AGC registers hold their hardware reset
    defaults — typically zero — and frames build correctly but radiate
    at near-zero power. The chip happily ACKs bulk-OUT writes regardless.
    """
    group = channel_to_group_8192e(channel)
    cck = efuse.cck_tx_power_index_A[group]

    # CCK rate 1M / MCS32 — power goes in bits[15:8].
    val32 = t.read32(REG_TX_AGC_A_CCK1_MCS32) & 0xFFFF00FF
    val32 |= (cck << 8)
    t.write32(REG_TX_AGC_A_CCK1_MCS32, val32 & 0xFFFFFFFF)

    # CCK rates 11M/B + 2-11M/A — power goes in bits[31:8] (3 byte lanes).
    val32 = t.read32(REG_TX_AGC_B_CCK11_A_CCK2_11) & 0xFF
    val32 |= (cck << 8) | (cck << 16) | (cck << 24)
    t.write32(REG_TX_AGC_B_CCK11_A_CCK2_11, val32 & 0xFFFFFFFF)

    # OFDM 6/9/12/18 + 24/36/48/54 — base + per-rate diff (path A, diff[0]).
    ofdmbase_a = (efuse.ht40_1s_tx_power_index_A[group] +
                  efuse.ofdm_tx_power_diff[0].a) & 0xFF
    t.write32(REG_TX_AGC_A_RATE18_06, _pack4(ofdmbase_a))
    t.write32(REG_TX_AGC_A_RATE54_24, _pack4(ofdmbase_a))

    # HT MCS rates — base + per-bandwidth diff (path A, ht20/ht40 diff[0]).
    mcsbase_a = efuse.ht40_1s_tx_power_index_A[group]
    if ht40:
        mcsbase_a += efuse.ht40_tx_power_diff[0].a
    else:
        mcsbase_a += efuse.ht20_tx_power_diff[0].a
    mcs_a = _pack4(mcsbase_a & 0xFF)
    t.write32(REG_TX_AGC_A_MCS03_MCS00, mcs_a)
    t.write32(REG_TX_AGC_A_MCS07_MCS04, mcs_a)
    t.write32(REG_TX_AGC_A_MCS11_MCS08, mcs_a)
    t.write32(REG_TX_AGC_A_MCS15_MCS12, mcs_a)

    # ---- path B (tx_paths > 1) — the same writes with diff index 1 ----
    cck_b = efuse.cck_tx_power_index_B[group]

    val32 = t.read32(REG_TX_AGC_B_CCK1_55_MCS32) & 0xFF
    val32 |= (cck_b << 8) | (cck_b << 16) | (cck_b << 24)
    t.write32(REG_TX_AGC_B_CCK1_55_MCS32, val32 & 0xFFFFFFFF)

    val32 = t.read32(REG_TX_AGC_B_CCK11_A_CCK2_11) & 0xFFFFFF00
    val32 |= cck_b
    t.write32(REG_TX_AGC_B_CCK11_A_CCK2_11, val32 & 0xFFFFFFFF)

    ofdmbase_b = (efuse.ht40_1s_tx_power_index_B[group] +
                  efuse.ofdm_tx_power_diff[1].b) & 0xFF
    t.write32(REG_TX_AGC_B_RATE18_06, _pack4(ofdmbase_b))
    t.write32(REG_TX_AGC_B_RATE54_24, _pack4(ofdmbase_b))

    mcsbase_b = efuse.ht40_1s_tx_power_index_B[group]
    if ht40:
        mcsbase_b += efuse.ht40_tx_power_diff[1].b
    else:
        mcsbase_b += efuse.ht20_tx_power_diff[1].b
    mcs_b = _pack4(mcsbase_b & 0xFF)
    t.write32(REG_TX_AGC_B_MCS03_MCS00, mcs_b)
    t.write32(REG_TX_AGC_B_MCS07_MCS04, mcs_b)
    t.write32(REG_TX_AGC_B_MCS11_MCS08, mcs_b)
    t.write32(REG_TX_AGC_B_MCS15_MCS12, mcs_b)

    logger.debug(
        "TX power set: ch %d (group=%d) cck_a=0x%02x ofdm_a=0x%02x mcs_a=0x%02x "
        "cck_b=0x%02x ofdm_b=0x%02x mcs_b=0x%02x",
        channel, group, cck, ofdmbase_a, mcsbase_a & 0xFF,
        cck_b, ofdmbase_b, mcsbase_b & 0xFF,
    )


def enable_cck_ofdm_block(t: RTL8192EUTransport) -> None:
    """Port of `core.c:4226-4233` — "Enable CCK and OFDM block".

    Sets `FPGA_RF_MODE_CCK | FPGA_RF_MODE_OFDM` (bits 24 + 25) in
    `REG_FPGA0_RF_MODE`. Without this both baseband blocks stay off and
    no received samples make it to the MAC RX FIFO — `REG_RCR` opens the
    gate but there's nothing flowing through it. `enable_rf` alone is not
    enough: it routes OFDM-to-RX, but OFDM itself must be turned on here.
    """
    val32 = t.read32(REG_FPGA0_RF_MODE)
    val32 |= FPGA_RF_MODE_CCK | FPGA_RF_MODE_OFDM
    t.write32(REG_FPGA0_RF_MODE, val32)


def enable_rf(t: RTL8192EUTransport) -> None:
    """Port of `rtl8192e_enable_rf` (8192e.c:1629-1673).

    UNPAUSES TX and configures the RF front-end: CCA wait extension, BT-coex
    IO select, PTA WLAN action, EEPRPad RFE control, both RFE buffer bits, the
    antenna-source mux and the external-switch pins. Called from
    `rtl8xxxu_start` (core.c:7364), AFTER `init_device`.
    """
    val32 = t.read32(REG_RX_WAIT_CCA)
    val32 |= (1 << 22) | (1 << 23)
    t.write32(REG_RX_WAIT_CCA, val32 & 0xFFFFFFFF)

    val8 = t.read8(REG_GPIO_MUXCFG)
    val8 |= 1 << 5
    t.write8(REG_GPIO_MUXCFG, val8 & 0xFF)

    # WLAN action by PTA.
    t.write8(REG_WLAN_ACT_CONTROL_8723B, 0x04)

    val32 = t.read32(REG_PWR_DATA)
    val32 |= PWR_DATA_EEPRPAD_RFE_CTRL_EN
    t.write32(REG_PWR_DATA, val32 & 0xFFFFFFFF)

    val32 = t.read32(REG_RFE_BUFFER)
    val32 |= (1 << 0) | (1 << 1)
    t.write32(REG_RFE_BUFFER, val32 & 0xFFFFFFFF)

    t.write8(REG_RFE_CTRL_ANTA_SRC, 0x77)

    val32 = t.read32(REG_LEDCFG0)
    val32 &= ~(1 << 24) & 0xFFFFFFFF
    val32 |= 1 << 23
    t.write32(REG_LEDCFG0, val32 & 0xFFFFFFFF)

    # Fix external switch Main->S1, Aux->S0.
    val8 = t.read8(REG_PAD_CTRL1)
    val8 &= ~(1 << 0) & 0xFF
    t.write8(REG_PAD_CTRL1, val8)

    # Fix transmission failure of rtl8192e.
    t.write8(REG_TXPAUSE, 0x00)


def disable_rf(t: RTL8192EUTransport) -> None:
    """Port of `rtl8xxxu_gen2_disable_rf` (core.c:3806-3813)."""
    val32 = t.read32(REG_RX_WAIT_CCA)
    val32 &= ~((1 << 22) | (1 << 23)) & 0xFFFFFFFF
    t.write32(REG_RX_WAIT_CCA, val32)


def usb_quirks(t: RTL8192EUTransport) -> None:
    """Port of `rtl8xxxu_gen2_usb_quirks` (core.c:3782-3789).

    `.usb_quirks` (`8192e.c:1739`) runs in `rtl8xxxu_init_device`'s always-taken
    `!macpower` block (`core.c:4054`), right after the auto-LLT build.
    """
    val32 = t.read32(REG_TXDMA_OFFSET_CHK)
    val32 |= TXDMA_OFFSET_DROP_DATA_EN
    t.write32(REG_TXDMA_OFFSET_CHK, val32 & 0xFFFFFFFF)


def set_rf_switch_ctrl(t: RTL8192EUTransport) -> None:
    """Port of the RFSW-control block (core.c:4007-4014).

    `REG_FPGA0_XAB_RF_SW_CTRL` selects the antenna switch pins. The kernel's
    `priv->no_pape` guard opens the PAPE bits; 8192eu never sets `no_pape`
    (the only setter is the 8188RU path, 8192c.c:460), so they are written.
    Runs right after `init_phy_rf` (`core.c:3993`).
    """
    val32 = (FPGA0_RF_TRSW | FPGA0_RF_TRSWB | FPGA0_RF_ANTSW | FPGA0_RF_ANTSWB |
             ((FPGA0_RF_ANTSW | FPGA0_RF_ANTSWB) << FPGA0_RF_BD_CTRL_SHIFT))
    val32 |= FPGA0_RF_PAPE | (FPGA0_RF_PAPE << FPGA0_RF_BD_CTRL_SHIFT)
    t.write32(REG_FPGA0_XAB_RF_SW_CTRL, val32 & 0xFFFFFFFF)


def enable_thermal_meter(t: RTL8192EUTransport) -> None:
    """Port of the `gen2_thermal_meter` branch (core.c:4297-4305).

    8192eu sets `.gen2_thermal_meter = 1` (`8192e.c:1752`); the chip is neither
    RTL8188F nor RTL8710B, so the `else` arm writes the literal `0x37cf8` to
    `RF6052_REG_T_METER_8723B` on RF path A.
    """
    write_rfreg(t, RF_A, RF6052_REG_T_METER_8723B, 0x37CF8)


def fix_ldpc_rx_hang(t: RTL8192EUTransport) -> None:
    """Port of the RTL8192E LDPC-rx-hang fix (core.c:4334-4342).

    Sets REG_8192E_LDOV12_CTRL = 0x75 (a read-back register), then rewrites
    `REG_AFE_MISC[18:13] = 0x3f` with `0x0007e000`. Runs after IQ cal.
    """
    val32 = t.read32(REG_AFE_MISC)
    t.write8(REG_8192E_LDOV12_CTRL, 0x75)
    val32 &= 0xFFF00FFF
    val32 |= 0x0007E000
    t.write32(REG_AFE_MISC, val32 & 0xFFFFFFFF)


def sync_cck_agc_report(t: RTL8192EUTransport) -> int:
    """Port of the RTL8192E CCK-AGC-report tail (core.c:4348-4367).

    Snapshot `REG_FPGA0_XA_HSSI_PARM2` BIT(9); force path B's
    `REG_FPGA0_XB_HSSI_PARM2` to match; then set/clear `AGC_RPT_CCK` in
    `REG_AGC_RPT`. The kernel comment (`core.c:4344-4347`) warns that
    0x824[9] / 0x82C[9] / 0xA80[7] must agree or CCK RSSI is wrong. Returns
    the 0/1 `cck_agc_report_type` pointer value for `rtl8192e_cck_rssi`.
    """
    val32 = t.read32(REG_FPGA0_XA_HSSI_PARM2)
    report_type = 1 if val32 & FPGA0_HSSI_PARM2_CCK_HIGH_PWR else 0

    val32 = t.read32(REG_FPGA0_XB_HSSI_PARM2)
    path_b_type = 1 if val32 & FPGA0_HSSI_PARM2_CCK_HIGH_PWR else 0
    if report_type != path_b_type:
        if report_type:
            val32 |= FPGA0_HSSI_PARM2_CCK_HIGH_PWR
        else:
            val32 &= ~FPGA0_HSSI_PARM2_CCK_HIGH_PWR & 0xFFFFFFFF
        t.write32(REG_FPGA0_XB_HSSI_PARM2, val32 & 0xFFFFFFFF)

    val32 = t.read32(REG_AGC_RPT)
    if report_type:
        val32 |= AGC_RPT_CCK
    else:
        val32 &= ~AGC_RPT_CCK & 0xFFFFFFFF
    t.write32(REG_AGC_RPT, val32 & 0xFFFFFFFF)

    return report_type
