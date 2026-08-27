"""RTL8188EUS PHY init (BB + AGC + RF path A).

Cleanroom port of:

* `rtl8188eu_init_phy_bb` — `8188e.c:582-603` (BB prep + 2 table loads)
* `rtl8188eu_init_phy_rf` — `8188e.c:605-608` (calls family-shared rf init)
* `rtl8xxxu_init_phy_regs` — `core.c:2228-2252` (BB/AGC table loader)
* `rtl8xxxu_init_phy_rf` — `core.c:2433-2495` (FPGA0 SW_CTRL wrap + RF table)
* `rtl8xxxu_init_rf_regs` — `core.c:2385-2431` (per-entry SIPI write w/ delay opcodes)
* `rtl8xxxu_write_rfreg` — `core.c:912-947` (SIPI 20-bit encode → LSSI_PARM)

No IQ or LC calibration here — those are separate fileops slots
(`.phy_iq_calibrate` / `.phy_lc_calibrate`) and run after this. Chip will
RX without them, just with degraded sensitivity. Calibration ports land
in a later milestone.
"""
from __future__ import annotations

import logging
import time

from .constants import (
    FPGA0_HSSI_3WIRE_ADDR_LEN,
    FPGA0_HSSI_3WIRE_DATA_LEN,
    FPGA0_LSSI_PARM_ADDR_SHIFT,
    FPGA0_LSSI_PARM_DATA_MASK,
    FPGA0_RF_RFENV,
    FPGA_RF_MODE_CCK,
    FPGA_RF_MODE_OFDM,
    OFDM_RF_PATH_RX_A,
    OFDM_RF_PATH_RX_MASK,
    OFDM_RF_PATH_TX_A,
    OFDM_RF_PATH_TX_MASK,
    REG_AFE_XTAL_CTRL,
    REG_FPGA0_RF_MODE,
    REG_FPGA0_XA_HSSI_PARM2,
    REG_FPGA0_XA_LSSI_PARM,
    REG_FPGA0_XA_RF_INT_OE,
    REG_FPGA0_XA_RF_SW_CTRL,
    REG_OFDM0_TRX_PATH_ENABLE,
    REG_RF_CTRL,
    REG_SYS_FUNC,
    REG_TX_AGC_A_CCK1_MCS32,
    REG_TX_AGC_A_MCS03_MCS00,
    REG_TX_AGC_A_MCS07_MCS04,
    REG_TX_AGC_A_MCS11_MCS08,
    REG_TX_AGC_A_MCS15_MCS12,
    REG_TX_AGC_A_RATE18_06,
    REG_TX_AGC_A_RATE54_24,
    REG_TX_AGC_B_CCK11_A_CCK2_11,
    REG_TXPAUSE,
    RF_ENABLE,
    RF_RSTB,
    RF_SDMRSTB,
    SYS_FUNC_BB_GLB_RSTN,
    SYS_FUNC_BBRSTB,
    SYS_FUNC_DIO_RF,
    SYS_FUNC_USBA,
    SYS_FUNC_USBD,
    XTAL0_MASK,
    XTAL0_SHIFT,
    XTAL1_MASK,
    XTAL1_SHIFT,
)
from .efuse import EfuseDefaults
from .phy_tables import (
    AGC_TABLE_8188E,
    PHY_INIT_TABLE_8188E,
    RADIO_A_INIT_TABLE_8188E,
)
from .transport import RTL8188EUSTransport

logger = logging.getLogger(__name__)

# RF path enum — 8188e is 1T1R so only RF_A matters; RF_B reserved for the
# family-shared API surface. Mirrors `enum rtl8xxxu_rfpath` in rtl8xxxu.h.
RF_A = 0
RF_B = 1


def init_phy_regs(t: RTL8188EUSTransport, table) -> int:
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


def write_rfreg(t: RTL8188EUSTransport, path: int, reg: int, data: int) -> None:
    """Port of `rtl8xxxu_write_rfreg` (core.c:912-947, 8188e path).

    Encodes ``(reg << 20) | (data & 0x000FFFFF)`` and writes it to the
    LSSI parameter register for `path`. 8188e is 1T1R so the family
    `rtl8xxxu_rfregs[path].lssiparm` lookup collapses to
    `REG_FPGA0_XA_LSSI_PARM` for the only RF path the chip has.
    """
    if path != RF_A:
        raise NotImplementedError("RTL8188EUS is 1T1R — only RF_A is supported")
    dataaddr = (reg << FPGA0_LSSI_PARM_ADDR_SHIFT) | (data & FPGA0_LSSI_PARM_DATA_MASK)
    t.write32(REG_FPGA0_XA_LSSI_PARM, dataaddr)
    time.sleep(0.000001)


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


def init_rf_regs(t: RTL8188EUSTransport, table, path: int = RF_A) -> int:
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


def init_phy_rf(t: RTL8188EUSTransport, table, path: int = RF_A) -> None:
    """Port of `rtl8xxxu_init_phy_rf` (core.c:2433-2495).

    Saves RFENV, sets up FPGA0 SW_CTRL / INT_OE / HSSI_PARM2 for SIPI
    access, runs the RF table, then restores RFENV. 8188e uses path A.
    """
    if path != RF_A:
        raise NotImplementedError("RTL8188EUS is 1T1R — only RF_A is supported")

    reg_sw_ctrl = REG_FPGA0_XA_RF_SW_CTRL
    reg_int_oe = REG_FPGA0_XA_RF_INT_OE
    reg_hssi_parm2 = REG_FPGA0_XA_HSSI_PARM2

    # Save RFENV.
    rfsi_rfenv = t.read16(reg_sw_ctrl) & FPGA0_RF_RFENV

    # SIPI access setup — two BIT-set pokes to INT_OE, then two BIT-clear
    # pokes to HSSI_PARM2 (3-wire serial mode).
    val32 = t.read32(reg_int_oe) | (1 << 20)
    t.write32(reg_int_oe, val32 & 0xFFFFFFFF)
    time.sleep(0.000001)

    val32 = t.read32(reg_int_oe) | (1 << 4)
    t.write32(reg_int_oe, val32 & 0xFFFFFFFF)
    time.sleep(0.000001)

    val32 = t.read32(reg_hssi_parm2) & ~FPGA0_HSSI_3WIRE_ADDR_LEN
    t.write32(reg_hssi_parm2, val32 & 0xFFFFFFFF)
    time.sleep(0.000001)

    val32 = t.read32(reg_hssi_parm2) & ~FPGA0_HSSI_3WIRE_DATA_LEN
    t.write32(reg_hssi_parm2, val32 & 0xFFFFFFFF)
    time.sleep(0.000001)

    # The RF init table itself.
    init_rf_regs(t, table, path)

    # Restore RFENV.
    val16 = t.read16(reg_sw_ctrl)
    val16 = (val16 & ~FPGA0_RF_RFENV & 0xFFFF) | rfsi_rfenv
    t.write16(reg_sw_ctrl, val16)


def init_phy_bb(t: RTL8188EUSTransport) -> None:
    """Port of `rtl8188eu_init_phy_bb` (8188e.c:582-603).

    BB prep pokes then load `phy_init_table` (192 entries) and `agc_table`
    (130 entries). The comment in the kernel says "Per vendor driver,
    run power sequence before init of RF" — the REG_RF_CTRL write here
    is that pre-RF power sequence.
    """
    # Step 1: SYS_FUNC |= BB_GLB_RSTN | BBRSTB | DIO_RF
    val16 = t.read16(REG_SYS_FUNC)
    val16 |= SYS_FUNC_BB_GLB_RSTN | SYS_FUNC_BBRSTB | SYS_FUNC_DIO_RF
    t.write16(REG_SYS_FUNC, val16)

    # Step 2: pre-RF power sequence — REG_RF_CTRL = ENABLE | RSTB | SDMRSTB
    t.write8(REG_RF_CTRL, RF_ENABLE | RF_RSTB | RF_SDMRSTB)

    # Step 3: SYS_FUNC = USBA | USBD | BB_GLB_RSTN | BBRSTB (byte write,
    # not OR-in)
    t.write8(REG_SYS_FUNC, SYS_FUNC_USBA | SYS_FUNC_USBD | SYS_FUNC_BB_GLB_RSTN | SYS_FUNC_BBRSTB)

    logger.debug("loading PHY_INIT_TABLE_8188E (%d entries)", len(PHY_INIT_TABLE_8188E))
    init_phy_regs(t, PHY_INIT_TABLE_8188E)

    logger.debug("loading AGC_TABLE_8188E (%d entries)", len(AGC_TABLE_8188E))
    init_phy_regs(t, AGC_TABLE_8188E)


def init_phy_rf_8188e(t: RTL8188EUSTransport) -> None:
    """Port of `rtl8188eu_init_phy_rf` (8188e.c:605-608)."""
    logger.debug("loading RADIO_A_INIT_TABLE_8188E (%d entries)", len(RADIO_A_INIT_TABLE_8188E))
    init_phy_rf(t, RADIO_A_INIT_TABLE_8188E, RF_A)


def set_crystal_cap(t: RTL8188EUSTransport, crystal_cap: int, prev_cap: int = 0) -> None:
    """Port of `rtl8188f_set_crystal_cap` (8188f.c:1650-1674).

    Writes the 6-bit EFUSE crystal-cap trim into both XTAL0 and XTAL1 of
    `REG_AFE_XTAL_CTRL`. The kernel skips the write when the cap already equals
    the cached value (`cfo->crystal_cap`); at cold bring-up that cache is 0, so a
    non-zero EFUSE cap always writes. `crystal_cap == 0` (no EFUSE / unparsed) is
    treated as "leave the hardware default" — the same skip the kernel takes.
    """
    if crystal_cap == prev_cap:
        return
    val32 = t.read32(REG_AFE_XTAL_CTRL)
    val32 &= ~(XTAL1_MASK | XTAL0_MASK) & 0xFFFFFFFF
    val32 |= (crystal_cap << XTAL1_SHIFT) | (crystal_cap << XTAL0_SHIFT)
    t.write32(REG_AFE_XTAL_CTRL, val32 & 0xFFFFFFFF)


def post_mac_init_phy(t: RTL8188EUSTransport, efuse: EfuseDefaults) -> None:
    """Run the full M3 PHY init sequence (BB + AGC + crystal cap + RF path A).

    Mirrors the kernel's generic `rtl8xxxu_init_phy_bb` wrapper (core.c:2310-2382):
    `fops->init_phy_bb` (BB + AGC tables) then `set_crystal_cap`, followed by the
    separate `fops->init_phy_rf`. The 1T2R patch + 8192E branch in that wrapper do
    not apply to the 1T1R 8188e.
    """
    init_phy_bb(t)
    set_crystal_cap(t, efuse.default_crystal_cap)
    init_phy_rf_8188e(t)


# ---- M8c TX power -------------------------------------------------


def channel_to_group_8188e(channel: int) -> tuple[int, int]:
    """Port of `rtl8188f_channel_to_group` (8188f.c:338-355).

    The 8188e fileops share this helper with 8188f. Maps a 2.4 GHz
    channel (1-14) to ``(group, cck_group)``:

        ch 1-2   → group 0
        ch 3-5   → group 1
        ch 6-8   → group 2
        ch 9-11  → group 3
        ch 12-13 → group 4
        ch 14    → cck_group = 5 (special)
    """
    if channel < 3:
        group = 0
    elif channel < 6:
        group = 1
    elif channel < 9:
        group = 2
    elif channel < 12:
        group = 3
    else:
        group = 4
    cck_group = 5 if channel == 14 else group
    return group, cck_group


def _pack4(byte_val: int) -> int:
    """Replicate `byte_val` across 4 byte lanes of a u32 (chip's standard
    "same power for all 4 rates in this register" pattern)."""
    b = byte_val & 0xFF
    return b | (b << 8) | (b << 16) | (b << 24)


def set_tx_power(
    t: RTL8188EUSTransport,
    channel: int,
    efuse: EfuseDefaults,
    *,
    ht40: bool = False,
) -> None:
    """Port of `rtl8188f_set_tx_power` (8188f.c:357-397).

    8188e shares this function with 8188f (fileops `.set_tx_power =
    rtl8188f_set_tx_power`, 8188e.c:1855). Writes per-channel-group
    CCK + OFDM + MCS power indices to the TX AGC registers based on
    EFUSE values.

    Without this call the TX AGC registers hold their hardware reset
    defaults — typically zero — and frames build correctly but radiate
    at near-zero power. The chip happily ACKs bulk-OUT writes regardless.
    """
    group, cck_group = channel_to_group_8188e(channel)
    cck = efuse.cck_tx_power_index_A[cck_group]

    # CCK rate 1M / MCS32 — power goes in bits[15:8].
    val32 = t.read32(REG_TX_AGC_A_CCK1_MCS32) & 0xFFFF00FF
    val32 |= (cck << 8)
    t.write32(REG_TX_AGC_A_CCK1_MCS32, val32 & 0xFFFFFFFF)

    # CCK rates 11M/B + 2-11M/A — power goes in bits[31:8] (3 byte lanes).
    val32 = t.read32(REG_TX_AGC_B_CCK11_A_CCK2_11) & 0xFF
    val32 |= (cck << 8) | (cck << 16) | (cck << 24)
    t.write32(REG_TX_AGC_B_CCK11_A_CCK2_11, val32 & 0xFFFFFFFF)

    # OFDM 6/9/12/18 + 24/36/48/54 — base + per-rate diff (path A).
    ofdmbase = (efuse.ht40_1s_tx_power_index_A[group] + efuse.ofdm_tx_power_diff_a) & 0xFF
    ofdm = _pack4(ofdmbase)
    t.write32(REG_TX_AGC_A_RATE18_06, ofdm)
    t.write32(REG_TX_AGC_A_RATE54_24, ofdm)

    # HT MCS rates — base + per-bandwidth diff (path A).
    mcsbase = efuse.ht40_1s_tx_power_index_A[group]
    if ht40:
        mcsbase += efuse.ht40_tx_power_diff_a
    else:
        mcsbase += efuse.ht20_tx_power_diff_a
    mcs = _pack4(mcsbase & 0xFF)
    t.write32(REG_TX_AGC_A_MCS03_MCS00, mcs)
    t.write32(REG_TX_AGC_A_MCS07_MCS04, mcs)
    t.write32(REG_TX_AGC_A_MCS11_MCS08, mcs)
    t.write32(REG_TX_AGC_A_MCS15_MCS12, mcs)

    logger.debug(
        "TX power set: ch %d (group=%d cck_group=%d) cck=0x%02x ofdm=0x%02x mcs=0x%02x",
        channel, group, cck_group, cck, ofdmbase & 0xFF, mcsbase & 0xFF,
    )


def enable_cck_ofdm_block(t: RTL8188EUSTransport) -> None:
    """Port of `core.c:4230-4232` — "Enable CCK and OFDM block".

    Sets `FPGA_RF_MODE_CCK | FPGA_RF_MODE_OFDM` (bits 24 + 25) in
    `REG_FPGA0_RF_MODE`. Without this both baseband blocks stay off and
    no received samples make it to the MAC RX FIFO — `REG_RCR` opens the
    gate but there's nothing flowing through it. Discovered 2026-05-18
    on the second M5 hw-test (first M5 fix `enable_rf` alone wasn't
    enough — the OFDM PATH enable connects OFDM-to-RX, but OFDM itself
    must be turned on here).
    """
    val32 = t.read32(REG_FPGA0_RF_MODE)
    val32 |= FPGA_RF_MODE_CCK | FPGA_RF_MODE_OFDM
    t.write32(REG_FPGA0_RF_MODE, val32)


def enable_rf(t: RTL8188EUSTransport) -> None:
    """Port of `rtl8188e_enable_rf` (8188e.c:1262-1274).

    Connects the OFDM baseband to RX/TX paths on radio A and unpauses TX.
    Note: this is **not enough on its own** — kernel `init_device` also
    enables the CCK + OFDM blocks via `REG_FPGA0_RF_MODE` (see
    `enable_cck_ofdm_block`). Both are needed for RX to deliver frames.

    Called from `rtl8xxxu_start` (core.c:7364), AFTER `init_device`.
    The kernel re-asserts REG_RF_CTRL (same write as in init_phy_bb) for
    belt-and-suspenders; we mirror that.
    """
    t.write8(REG_RF_CTRL, RF_ENABLE | RF_RSTB | RF_SDMRSTB)

    val32 = t.read32(REG_OFDM0_TRX_PATH_ENABLE)
    val32 &= ~(OFDM_RF_PATH_RX_MASK | OFDM_RF_PATH_TX_MASK) & 0xFFFFFFFF
    val32 |= OFDM_RF_PATH_RX_A | OFDM_RF_PATH_TX_A
    t.write32(REG_OFDM0_TRX_PATH_ENABLE, val32)

    t.write8(REG_TXPAUSE, 0x00)
