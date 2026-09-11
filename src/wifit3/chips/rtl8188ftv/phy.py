"""RTL8188FTV PHY init + channel tune + RF enable.

Mirror of:
* `rtl8188fu_init_phy_bb` — `8188f.c:751-776` (BB + AGC table writes)
* `rtl8188fu_init_phy_rf` — `8188f.c:778-788` (RF path-A table write)
* `rtl8188f_set_crystal_cap` — `8188f.c:1650-1674` (XTAL0/XTAL1 field write)
* `rtl8188f_set_tx_power` — `8188f.c:358-397` (per-channel-group power)
* `rtl8188f_enable_rf` — `8188f.c:1582-1619` (RF enable + OFDM path-A)
* `rtl8188fu_config_channel` — `8188f.c:514-643` (20 MHz channel tune)
"""
from __future__ import annotations

import logging
import time

from .constants import (
    FPGA0_HSSI_3WIRE_ADDR_LEN,
    FPGA0_HSSI_3WIRE_DATA_LEN,
    FPGA0_RF_RFENV,
    FPGA_RF_MODE,
    MODE_AG_BW_20MHZ_8723B,
    MODE_AG_CHANNEL_MASK,
    OFDM_RF_PATH_RX_A,
    OFDM_RF_PATH_TX_A,
    REG_AFE_XTAL_CTRL,
    REG_FPGA0_RF_MODE,
    REG_FPGA0_XA_HSSI_PARM2,
    REG_FPGA0_XA_RF_INT_OE,
    REG_FPGA0_XA_RF_SW_CTRL,
    REG_FPGA1_RF_MODE,
    REG_OFDM0_RX_D_SYNC_PATH,
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
    RF6052_REG_GAIN_CCA,
    RF6052_REG_IQADJ_G1,
    RF6052_REG_MODE_AG,
    RF6052_REG_RX_BB2,
    RF6052_REG_RX_G2,
    RF6052_REG_RXG_MIX_SWBW,
    RF_ENABLE,
    RF_RSTB,
    RF_SDMRSTB,
    SYS_FUNC_BBRSTB,
    SYS_FUNC_BB_GLB_RSTN,
    SYS_FUNC_DIO_RF,
    SYS_FUNC_USBA,
    SYS_FUNC_USBD,
    XTAL0_MASK,
    XTAL0_SHIFT,
    XTAL1_MASK,
    XTAL1_SHIFT,
)
from .phy_tables import agc, phy, radio_a, radio_a_cut_b
from .transport import RTL8188FTVTransport

logger = logging.getLogger(__name__)


# ---- BB init (8188f.c:751-776) ---------------------------------------


def init_phy_bb(t: RTL8188FTVTransport) -> None:
    """Mirror of `rtl8188fu_init_phy_bb` (`8188f.c:751-776`).

    Enables BB + RF, sets IQADJ, then writes the PHY init and AGC tables.
    """
    val16 = t.read16(REG_SYS_FUNC)
    val16 |= SYS_FUNC_BB_GLB_RSTN | SYS_FUNC_BBRSTB | SYS_FUNC_DIO_RF
    t.write16(REG_SYS_FUNC, val16)

    val8 = RF_ENABLE | RF_RSTB | RF_SDMRSTB
    t.write8(REG_RF_CTRL, val8)

    time.sleep(0.00001)

    t.write_rfreg(0, RF6052_REG_IQADJ_G1, 0x780)

    val8 = SYS_FUNC_BB_GLB_RSTN | SYS_FUNC_BBRSTB | SYS_FUNC_USBA | SYS_FUNC_USBD
    t.write8(REG_SYS_FUNC, val8)

    for reg, val in phy:
        if reg == 0xFFFF and val == 0xFFFF_FFFF:
            break
        t.write32(reg, val)

    for reg, val in agc:
        if reg == 0xFFFF and val == 0xFFFF_FFFF:
            break
        t.write32(reg, val)


# ---- RF init (8188f.c:778-788 + core.c:2413-2475) -----------------


def init_phy_rf(t: RTL8188FTVTransport, chip_cut: int) -> None:
    """Mirror of `rtl8188fu_init_phy_rf` + `rtl8xxxu_init_phy_rf`.

    The RF-interface enable preamble (RF_RFENV backup, INT_OE bits,
    HSSI_PARM2 addr/data word-length clears) is from core.c:2434-2467;
    the table loop from core.c:2391-2411; the RFENV restore from
    core.c:2469-2474.  RFREG values land via the LSSI data register,
    so all of this is plain 32-bit LSSI writes.
    """
    tbl = radio_a_cut_b if chip_cut == 1 else radio_a

    rfsi_rfenv = t.read16(REG_FPGA0_XA_RF_SW_CTRL) & FPGA0_RF_RFENV

    val32 = t.read32(REG_FPGA0_XA_RF_INT_OE)
    val32 |= 1 << 20
    t.write32(REG_FPGA0_XA_RF_INT_OE, val32)

    val32 = t.read32(REG_FPGA0_XA_RF_INT_OE)
    val32 |= 1 << 4
    t.write32(REG_FPGA0_XA_RF_INT_OE, val32)

    val32 = t.read32(REG_FPGA0_XA_HSSI_PARM2)
    val32 &= ~FPGA0_HSSI_3WIRE_ADDR_LEN
    t.write32(REG_FPGA0_XA_HSSI_PARM2, val32)

    val32 = t.read32(REG_FPGA0_XA_HSSI_PARM2)
    val32 &= ~FPGA0_HSSI_3WIRE_DATA_LEN
    t.write32(REG_FPGA0_XA_HSSI_PARM2, val32)

    for reg, val in tbl:
        if reg == 0xFF and val == 0xFFFF_FFFF:
            break
        if reg == 0xFE:
            time.sleep(0.05)
            continue
        if reg == 0xFD:
            time.sleep(0.005)
            continue
        if reg == 0xFC:
            time.sleep(0.001)
            continue
        if reg == 0xFB:
            time.sleep(0.00005)
            continue
        if reg == 0xFA:
            time.sleep(0.000005)
            continue
        if reg == 0xF9:
            time.sleep(0.000001)
            continue
        t.write_rfreg(0, reg, val)

    val16 = t.read16(REG_FPGA0_XA_RF_SW_CTRL)
    val16 &= ~FPGA0_RF_RFENV
    val16 |= rfsi_rfenv
    t.write16(REG_FPGA0_XA_RF_SW_CTRL, val16)


# ---- crystal cap (8188f.c:1650-1674) ---------------------------------


def set_crystal_cap(t: RTL8188FTVTransport, crystal_cap: int) -> None:
    """Mirror of `rtl8188f_set_crystal_cap` (`8188f.c:1650-1674`)."""
    val32 = t.read32(REG_AFE_XTAL_CTRL)
    val32 &= ~(XTAL0_MASK | XTAL1_MASK)
    val32 |= (crystal_cap << XTAL0_SHIFT) | (crystal_cap << XTAL1_SHIFT)
    t.write32(REG_AFE_XTAL_CTRL, val32)


# ---- M2 composite gate -----------------------------------------------


def post_mac_init_phy(t: RTL8188FTVTransport, chip_cut: int,
                      crystal_cap: int = 0) -> None:
    """Combine init_phy_bb + crystal_cap + init_phy_rf.

    This is the M2 replay unit — byte-for-byte verified against the
    capture's post-MAC-init region.
    """
    init_phy_bb(t)
    set_crystal_cap(t, crystal_cap)
    init_phy_rf(t, chip_cut)


# ---- TX power (8188f.c:338-397) --------------------------------------


def set_tx_power(t: RTL8188FTVTransport, channel: int, efuse=None) -> None:
    """Mirror of `rtl8188f_set_tx_power` (`8188f.c:358-397`)."""
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

    if efuse is None:
        return

    cck = efuse.cck_tx_power_index_A[cck_group]
    val32 = t.read32(REG_TX_AGC_A_CCK1_MCS32)
    val32 &= 0xFFFF00FF
    val32 |= cck << 8
    t.write32(REG_TX_AGC_A_CCK1_MCS32, val32)

    val32 = t.read32(REG_TX_AGC_B_CCK11_A_CCK2_11)
    val32 &= 0xFF
    val32 |= (cck << 8) | (cck << 16) | (cck << 24)
    t.write32(REG_TX_AGC_B_CCK11_A_CCK2_11, val32)

    ofdmbase = efuse.ht40_1s_tx_power_index_A[group] + efuse.ofdm_tx_power_diff_a
    ofdm = ofdmbase | (ofdmbase << 8) | (ofdmbase << 16) | (ofdmbase << 24)
    t.write32(REG_TX_AGC_A_RATE18_06, ofdm)
    t.write32(REG_TX_AGC_A_RATE54_24, ofdm)

    mcsbase = efuse.ht40_1s_tx_power_index_A[group] + efuse.ht20_tx_power_diff_a
    mcs = mcsbase | (mcsbase << 8) | (mcsbase << 16) | (mcsbase << 24)
    t.write32(REG_TX_AGC_A_MCS03_MCS00, mcs)
    t.write32(REG_TX_AGC_A_MCS07_MCS04, mcs)
    t.write32(REG_TX_AGC_A_MCS11_MCS08, mcs)
    t.write32(REG_TX_AGC_A_MCS15_MCS12, mcs)


# ---- RF enable (8188f.c:1582-1619) -----------------------------------


def enable_rf(t: RTL8188FTVTransport) -> None:
    """Mirror of `rtl8188f_enable_rf` (`8188f.c:1582-1619`).

    Reads EFUSE BB gain trim (if burned), then enables the RF path-A
    and sets the OFDM TX/RX path mask.
    """
    t.write8(REG_RF_CTRL, RF_ENABLE | RF_RSTB | RF_SDMRSTB)

    val32 = t.read32(REG_OFDM0_TRX_PATH_ENABLE)
    val32 &= ~(0x0F | 0xF0)
    val32 |= OFDM_RF_PATH_RX_A | OFDM_RF_PATH_TX_A
    t.write32(REG_OFDM0_TRX_PATH_ENABLE, val32)

    t.write8(0x0525, 0x00)


# ---- channel tune 2.4 GHz 20 MHz (8188f.c:514-643) ------------------


def set_channel_2g_20mhz(t: RTL8188FTVTransport, channel: int) -> None:
    """Mirror of `rtl8188fu_config_channel` for 20 MHz bandwidth."""
    val32 = t.read_rfreg(0, RF6052_REG_MODE_AG)
    val32 &= ~MODE_AG_CHANNEL_MASK
    val32 |= channel
    t.write_rfreg(0, RF6052_REG_MODE_AG, val32)

    val32 = t.read32(REG_FPGA0_RF_MODE)
    val32 &= ~FPGA_RF_MODE
    t.write32(REG_FPGA0_RF_MODE, val32)

    val32 = t.read32(REG_FPGA1_RF_MODE)
    val32 &= ~FPGA_RF_MODE
    t.write32(REG_FPGA1_RF_MODE, val32)

    val32 = t.read32(REG_FPGA0_RF_MODE)
    val32 |= (7 << 8)
    t.write32(REG_FPGA0_RF_MODE, val32)

    val32 = t.read32(REG_FPGA0_RF_MODE)
    val32 |= (1 << 14) | (1 << 12)
    val32 &= ~(1 << 13)
    t.write32(REG_FPGA0_RF_MODE, val32)

    val32 = t.read32(REG_OFDM0_RX_D_SYNC_PATH)
    val32 &= ~((3 << 30))
    t.write32(REG_OFDM0_RX_D_SYNC_PATH, val32)

    val32 = t.read32(REG_OFDM0_RX_D_SYNC_PATH)
    val32 &= ~(1 << 29)
    val32 |= (1 << 28)
    t.write32(REG_OFDM0_RX_D_SYNC_PATH, val32)

    val32 = t.read32(REG_OFDM0_RX_D_SYNC_PATH)
    val32 &= ~(1 << 19)
    t.write32(REG_OFDM0_RX_D_SYNC_PATH, val32)

    val32 = t.read32(REG_OFDM0_RX_D_SYNC_PATH)
    val32 &= ~((0xF << 20))
    val32 |= (1 << 21) | (1 << 20)
    t.write32(REG_OFDM0_RX_D_SYNC_PATH, val32)

    val32 = channel | MODE_AG_BW_20MHZ_8723B
    t.write_rfreg(0, RF6052_REG_MODE_AG, val32)

    t.write_rfreg(0, RF6052_REG_RXG_MIX_SWBW, 0x00065)
    t.write_rfreg(0, RF6052_REG_RX_BB2, 0x00000)
    t.write_rfreg(0, RF6052_REG_GAIN_CCA, 0x00140)
    t.write_rfreg(0, RF6052_REG_RX_G2, 0x01c6c)
