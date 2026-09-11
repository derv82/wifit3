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
    BIT,
    FPGA0_HSSI_3WIRE_ADDR_LEN,
    FPGA0_HSSI_3WIRE_DATA_LEN,
    FPGA0_HSSI_PARM1_PI,
    FPGA0_HSSI_PARM2_ADDR_MASK,
    FPGA0_HSSI_PARM2_ADDR_SHIFT,
    FPGA0_HSSI_PARM2_EDGE_READ,
    FPGA0_RF_RFENV,
    FPGA_RF_MODE,
    FWHW_TXQ_CTRL_XMIT_MGMT_ACK,
    IQK_MAX_TOLERANCE,
    MODE_AG_BW_20MHZ_8723B,
    MODE_AG_CHANNEL_MASK,
    NAV_UPPER_UNIT,
    OFDM_LSTF_MASK,
    OFDM_RF_PATH_RX_A,
    OFDM_RF_PATH_RX_MASK,
    OFDM_RF_PATH_TX_A,
    OFDM_RF_PATH_TX_MASK,
    PPG_BB_GAIN_2G_TXA_OFFSET_8188F,
    PPG_BB_GAIN_2G_TX_OFFSET_MASK,
    REG_AFE_XTAL_CTRL,
    REG_FPGA0_IQK,
    REG_FPGA0_RF_MODE,
    REG_FPGA0_XA_HSSI_PARM1,
    REG_FPGA0_XA_HSSI_PARM2,
    REG_FPGA0_XA_LSSI_PARM,
    REG_FPGA0_XA_RF_INT_OE,
    REG_FPGA0_XA_RF_SW_CTRL,
    REG_FPGA0_XCD_RF_SW_CTRL,
    REG_FPGA1_RF_MODE,
    REG_FWHW_TXQ_CTRL,
    REG_HSPI_XA_READBACK,
    REG_IQK_AGC_PTS,
    REG_IQK_AGC_RSP,
    REG_NAV_UPPER,
    REG_OFDM0_ENERGY_CCA_THRES,
    REG_OFDM0_RX_D_SYNC_PATH,
    REG_OFDM0_RX_IQ_EXT_ANTA,
    REG_OFDM0_TR_MUX_PAR,
    REG_OFDM0_TRX_PATH_ENABLE,
    REG_OFDM0_XA_AGC_CORE1,
    REG_OFDM0_XA_RX_IQ_IMBALANCE,
    REG_OFDM0_XA_TX_IQ_IMBALANCE,
    REG_OFDM0_XC_TX_AFE,
    REG_OFDM1_CFO_TRACKING,
    REG_OFDM1_LSTF,
    REG_FPGA0_XA_LSSI_READBACK,
    REG_FPGA0_XB_HSSI_PARM1,
    REG_RF_CTRL,
    REG_RX_IQK,
    REG_RX_IQK_PI_A,
    REG_RX_IQK_TONE_A,
    REG_RX_POWER_AFTER_IQK_A_2,
    REG_RX_POWER_BEFORE_IQK_A_2,
    REG_S0S1_PATH_SWITCH,
    REG_SYS_FUNC,
    REG_TX_AGC_A_CCK1_MCS32,
    REG_TX_AGC_A_MCS03_MCS00,
    REG_TX_AGC_A_MCS07_MCS04,
    REG_TX_AGC_A_MCS11_MCS08,
    REG_TX_AGC_A_MCS15_MCS12,
    REG_TX_AGC_A_RATE18_06,
    REG_TX_AGC_A_RATE54_24,
    REG_TX_AGC_B_CCK11_A_CCK2_11,
    REG_TX_IQK,
    REG_TX_IQK_PI_A,
    REG_TX_IQK_TONE_A,
    REG_TX_POWER_AFTER_IQK_A,
    REG_TX_POWER_BEFORE_IQK_A,
    REG_TXPAUSE,
    REG_TXPTCL_CTRL,
    RF6052_REG_GAIN_CCA,
    RF6052_REG_IQADJ_G1,
    RF6052_REG_MODE_AG,
    RF6052_REG_PAD_TXG,
    RF6052_REG_RCK_OS,
    RF6052_REG_RX_BB2,
    RF6052_REG_RX_G2,
    RF6052_REG_RXG_MIX_SWBW,
    RF6052_REG_S0S1,
    RF6052_REG_T_METER_8723B,
    RF6052_REG_TXM_IDAC,
    RF6052_REG_TXPA_G1,
    RF6052_REG_TXPA_G2,
    RF6052_REG_UNKNOWN_55,
    RF6052_REG_WE_LUT,
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
from .efuse import read_efuse_byte
from .phy_tables import agc, phy, radio_a, radio_a_cut_b
from .transport import RTL8188FTVTransport

logger = logging.getLogger(__name__)

# ---- HSSI 3-wire RF register access (core.c:867-905) -----------------


def read_rfreg(t: RTL8188FTVTransport, path: int, reg: int) -> int:
    """Read RF register via HSSI 3-wire interface (path-A only).

    Kernel: `rtl8xxxu_read_rfreg` core.c:867-905.
    Produces 6 ops: R 0824, W 0824, W 0824, W 0824, R 0820, R 08b8/08a0.
    """
    hssia = t.read32(REG_FPGA0_XA_HSSI_PARM2)

    # Clear edge bit
    hssia &= ~FPGA0_HSSI_PARM2_EDGE_READ
    t.write32(REG_FPGA0_XA_HSSI_PARM2, hssia)
    time.sleep(0.000010)

    # Set address
    val32 = hssia & ~FPGA0_HSSI_PARM2_ADDR_MASK
    val32 |= (reg << FPGA0_HSSI_PARM2_ADDR_SHIFT)
    val32 |= FPGA0_HSSI_PARM2_EDGE_READ
    t.write32(REG_FPGA0_XA_HSSI_PARM2, val32)
    time.sleep(0.000100)

    # Set edge high
    hssia |= FPGA0_HSSI_PARM2_EDGE_READ
    t.write32(REG_FPGA0_XA_HSSI_PARM2, hssia)
    time.sleep(0.000010)

    # Read result
    val32 = t.read32(REG_FPGA0_XA_HSSI_PARM1)
    if val32 & FPGA0_HSSI_PARM1_PI:
        retval = t.read32(REG_HSPI_XA_READBACK)
    else:
        retval = t.read32(REG_FPGA0_XA_LSSI_READBACK)
    return retval & 0xfffff


def write_rfreg(t: RTL8188FTVTransport, path: int, reg: int, data: int) -> None:
    """Write RF register via LSSI single-op interface (path-A only).

    Kernel: `rtl8xxxu_write_rfreg` core.c:907-919.
    Produces 1 op: W 0840.
    """
    data &= 0x000FFFFF
    addr = (reg << 20) | data
    t.write32(REG_FPGA0_XA_LSSI_PARM, addr)


# ---- LC calibration (8188f.c:790-833) --------------------------------


def lc_calibrate(t: RTL8188FTVTransport) -> None:
    """Mirror of `rtl8188f_phy_lc_calibrate` (8188f.c:790-833).

    Produces 47 ops against the cold-boot capture.
    """
    lstf = t.read32(REG_OFDM1_LSTF)

    if lstf & OFDM_LSTF_MASK:
        val32 = lstf & ~OFDM_LSTF_MASK
        t.write32(REG_OFDM1_LSTF, val32)
    else:
        t.write8(REG_TXPAUSE, 0xFF)

    rf_amode = read_rfreg(t, 0, RF6052_REG_MODE_AG)
    write_rfreg(t, 0, RF6052_REG_MODE_AG, rf_amode | 0x08000)

    for _ in range(100):
        if (read_rfreg(t, 0, RF6052_REG_MODE_AG) & 0x08000) == 0:
            break
        time.sleep(0.010)

    write_rfreg(t, 0, RF6052_REG_MODE_AG, rf_amode)

    if lstf & OFDM_LSTF_MASK:
        t.write32(REG_OFDM1_LSTF, lstf)
    else:
        t.write8(REG_TXPAUSE, 0x00)


# ---- IQ calibration (8188f.c:835-1299) --------------------------------

# register arrays — kernel core.c:2996 / 8188f.c:1067-1069
_ADDA_REGS = [
    0x085c, 0x0e6c, 0x0e70, 0x0e74, 0x0e78, 0x0e7c,
    0x0e80, 0x0e84, 0x0e88, 0x0e8c, 0x0ed0, 0x0ed4,
    0x0ed8, 0x0edc, 0x0ee0, 0x0eec,
]
_MAC_REGS = [0x0522, 0x0550, 0x0551, 0x0040]
_BB_REGS = [0x0c04, 0x0c08, 0x0874, 0x0b68, 0x0b6c, 0x0870, 0x0860, 0x0864, 0x0800]
_ADDA_1T_PATH_ON = 0x03c00014   # 8188f.c:1756
_ADDA_1T_INIT = 0x03c00014      # 8188f.c:1755 (== path_on for 1T)
_IQK_BB_REGS = [0x0c14, 0x0c1c, 0x0c4c, 0x0c78, 0x0c80, 0x0c88, 0x0c94, 0x0c9c, 0x0ca0]  # core.c:602-610


def _iqk_path_a(t: RTL8188FTVTransport, lok_result: list[int]) -> int:
    """TX IQK path-A (8188f.c:835-913).

    Returns 0x01 on success, 0x00 on failure; stores LOK result.
    """
    val32 = t.read32(REG_FPGA0_IQK)
    val32 &= 0x000000ff
    t.write32(REG_FPGA0_IQK, val32)

    val32 = read_rfreg(t, 0, RF6052_REG_WE_LUT)
    val32 |= 0x80000
    write_rfreg(t, 0, RF6052_REG_WE_LUT, val32)
    write_rfreg(t, 0, RF6052_REG_RCK_OS, 0x20000)
    write_rfreg(t, 0, RF6052_REG_TXPA_G1, 0x0000f)
    write_rfreg(t, 0, RF6052_REG_TXPA_G2, 0x07ff7)

    write_rfreg(t, 0, RF6052_REG_GAIN_CCA, 0x980)
    write_rfreg(t, 0, RF6052_REG_PAD_TXG, 0x5102a)

    val32 = t.read32(REG_FPGA0_IQK)
    val32 &= 0x000000ff
    val32 |= 0x80800000
    t.write32(REG_FPGA0_IQK, val32)

    t.write32(REG_TX_IQK_TONE_A, 0x18008c1c)
    t.write32(REG_RX_IQK_TONE_A, 0x38008c1c)

    t.write32(REG_TX_IQK_PI_A, 0x821403ff)
    t.write32(REG_RX_IQK_PI_A, 0x28160000)

    t.write32(REG_IQK_AGC_RSP, 0x00462911)

    t.write32(REG_IQK_AGC_PTS, 0xf9000000)
    t.write32(REG_IQK_AGC_PTS, 0xf8000000)

    val32 = t.read32(REG_FPGA0_IQK)
    val32 &= 0x000000ff
    t.write32(REG_FPGA0_IQK, val32)

    write_rfreg(t, 0, RF6052_REG_GAIN_CCA, 0x180)

    lok_result[0] = read_rfreg(t, 0, RF6052_REG_TXM_IDAC)

    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = t.read32(REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = t.read32(REG_TX_POWER_AFTER_IQK_A)

    result = 0
    if not (reg_eac & BIT(28)):
        if (reg_e94 & 0x03ff0000) != 0x01420000:
            if (reg_e9c & 0x03ff0000) != 0x00420000:
                result |= 0x01

    return result


def _rx_iqk_path_a(t: RTL8188FTVTransport, lok_result: int) -> int:
    """RX IQK path-A (8188f.c:916-1058).

    Returns 0x01 (TX pass), 0x03 (TX+RX pass), or 0x00.
    """
    val32 = t.read32(REG_FPGA0_IQK)
    val32 &= 0x000000ff
    t.write32(REG_FPGA0_IQK, val32)

    val32 = read_rfreg(t, 0, RF6052_REG_WE_LUT)
    val32 |= 0x80000
    write_rfreg(t, 0, RF6052_REG_WE_LUT, val32)
    write_rfreg(t, 0, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, 0, RF6052_REG_TXPA_G1, 0x0000f)
    write_rfreg(t, 0, RF6052_REG_TXPA_G2, 0xf1173)

    write_rfreg(t, 0, RF6052_REG_GAIN_CCA, 0x980)
    write_rfreg(t, 0, RF6052_REG_PAD_TXG, 0x5102a)

    val32 = t.read32(REG_FPGA0_IQK)
    val32 &= 0x000000ff
    val32 |= 0x80800000
    t.write32(REG_FPGA0_IQK, val32)

    t.write32(REG_TX_IQK, 0x01007c00)
    t.write32(REG_RX_IQK, 0x01004800)

    t.write32(REG_TX_IQK_TONE_A, 0x10008c1c)
    t.write32(REG_RX_IQK_TONE_A, 0x30008c1c)

    t.write32(REG_TX_IQK_PI_A, 0x82160fff)
    t.write32(REG_RX_IQK_PI_A, 0x28160000)

    t.write32(REG_IQK_AGC_RSP, 0x00462911)

    t.write32(REG_IQK_AGC_PTS, 0xf9000000)
    t.write32(REG_IQK_AGC_PTS, 0xf8000000)

    val32 = t.read32(REG_FPGA0_IQK)
    val32 &= 0x000000ff
    t.write32(REG_FPGA0_IQK, val32)

    write_rfreg(t, 0, RF6052_REG_GAIN_CCA, 0x180)

    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = t.read32(REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = t.read32(REG_TX_POWER_AFTER_IQK_A)

    result = 0
    if not (reg_eac & BIT(28)):
        if (reg_e94 & 0x03ff0000) != 0x01420000:
            if (reg_e9c & 0x03ff0000) != 0x00420000:
                result |= 0x01
    else:
        return result

    val32 = 0x80007c00 | (reg_e94 & 0x3ff0000) | ((reg_e9c & 0x3ff0000) >> 16)
    t.write32(REG_TX_IQK, val32)

    # ---- Modify RX IQK mode table ----
    val32 = t.read32(REG_FPGA0_IQK)
    val32 &= 0x000000ff
    t.write32(REG_FPGA0_IQK, val32)

    val32 = read_rfreg(t, 0, RF6052_REG_WE_LUT)
    val32 |= 0x80000
    write_rfreg(t, 0, RF6052_REG_WE_LUT, val32)
    write_rfreg(t, 0, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, 0, RF6052_REG_TXPA_G1, 0x0000f)
    write_rfreg(t, 0, RF6052_REG_TXPA_G2, 0xf7ff2)

    write_rfreg(t, 0, RF6052_REG_GAIN_CCA, 0x980)
    write_rfreg(t, 0, RF6052_REG_PAD_TXG, 0x51000)

    val32 = t.read32(REG_FPGA0_IQK)
    val32 &= 0x000000ff
    val32 |= 0x80800000
    t.write32(REG_FPGA0_IQK, val32)

    t.write32(REG_RX_IQK, 0x01004800)

    t.write32(REG_TX_IQK_TONE_A, 0x30008c1c)
    t.write32(REG_RX_IQK_TONE_A, 0x10008c1c)

    t.write32(REG_TX_IQK_PI_A, 0x82160000)
    t.write32(REG_RX_IQK_PI_A, 0x281613ff)

    t.write32(REG_IQK_AGC_RSP, 0x0046a911)

    t.write32(REG_IQK_AGC_PTS, 0xf9000000)
    t.write32(REG_IQK_AGC_PTS, 0xf8000000)

    val32 = t.read32(REG_FPGA0_IQK)
    val32 &= 0x000000ff
    t.write32(REG_FPGA0_IQK, val32)

    write_rfreg(t, 0, RF6052_REG_GAIN_CCA, 0x180)

    write_rfreg(t, 0, RF6052_REG_TXM_IDAC, lok_result)

    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_ea4 = t.read32(REG_RX_POWER_BEFORE_IQK_A_2)

    if not (reg_eac & BIT(27)):
        if (reg_ea4 & 0x03ff0000) != 0x01320000:
            if (reg_eac & 0x03ff0000) != 0x00360000:
                result |= 0x02

    return result


def _gen2_simularity_compare(result: list[list[int]], c1: int, c2: int) -> bool:
    """IQK similarity compare (core.c:2910-2966).

    Pure compute — issues no register ops. Returns True when the two
    trials agree within MAX_TOLERANCE.
    """
    bound = 4
    simubitmap = 0
    candidate = [-1, -1]

    for i in range(bound):
        if i & 1:
            if result[c1][i] & 0x00000200:
                tmp1 = result[c1][i] | 0xfffffc00
            else:
                tmp1 = result[c1][i]
            if result[c2][i] & 0x00000200:
                tmp2 = result[c2][i] | 0xfffffc00
            else:
                tmp2 = result[c2][i]
        else:
            tmp1 = result[c1][i]
            tmp2 = result[c2][i]

        diff = abs(tmp1 - tmp2)

        if diff > IQK_MAX_TOLERANCE:
            if (i == 2 or i == 6) and not simubitmap:
                if result[c1][i] + result[c1][i + 1] == 0:
                    candidate[i // 4] = c2
                elif result[c2][i] + result[c2][i + 1] == 0:
                    candidate[i // 4] = c1
                else:
                    simubitmap |= 1 << i
            else:
                simubitmap |= 1 << i

    if simubitmap == 0:
        for i in range(bound // 4):
            if candidate[i] >= 0:
                for j in range(i * 4, (i + 1) * 4 - 2):
                    result[3][j] = result[candidate[i]][j]
                return False
        return True
    else:
        if not (simubitmap & 0x03):
            for i in range(2):
                if candidate[i] >= 0:
                    for j in range(i * 4, (i + 1) * 4 - 2):
                        result[3][j] = result[candidate[i]][j]
            return False
        return True


def _fill_iqk_matrix_a(
    t: RTL8188FTVTransport, result: list[int], tx_only: bool,
) -> None:
    """Fill IQK matrix for path-A (core.c:2696-2758)."""
    val32 = t.read32(REG_OFDM0_XA_TX_IQ_IMBALANCE)
    oldval = val32 >> 22

    x = result[0]
    if (x & 0x00000200) != 0:
        x = x | 0xfffffc00
    tx0_a = (x * oldval) >> 8

    val32 = t.read32(REG_OFDM0_XA_TX_IQ_IMBALANCE)
    val32 &= ~0x3ff
    val32 |= tx0_a
    t.write32(REG_OFDM0_XA_TX_IQ_IMBALANCE, val32)

    val32 = t.read32(REG_OFDM0_ENERGY_CCA_THRES)
    val32 &= ~BIT(31)
    if (x * oldval >> 7) & 0x1:
        val32 |= BIT(31)
    t.write32(REG_OFDM0_ENERGY_CCA_THRES, val32)

    y = result[1]
    if (y & 0x00000200) != 0:
        y = y | 0xfffffc00
    tx0_c = (y * oldval) >> 8

    val32 = t.read32(REG_OFDM0_XC_TX_AFE)
    val32 &= ~0xf0000000
    val32 |= (((tx0_c & 0x3c0) >> 6) << 28)
    t.write32(REG_OFDM0_XC_TX_AFE, val32)

    val32 = t.read32(REG_OFDM0_XA_TX_IQ_IMBALANCE)
    val32 &= ~0x003f0000
    val32 |= ((tx0_c & 0x3f) << 16)
    t.write32(REG_OFDM0_XA_TX_IQ_IMBALANCE, val32)

    val32 = t.read32(REG_OFDM0_ENERGY_CCA_THRES)
    val32 &= ~BIT(29)
    if (y * oldval >> 7) & 0x1:
        val32 |= BIT(29)
    t.write32(REG_OFDM0_ENERGY_CCA_THRES, val32)

    if tx_only:
        return

    reg = result[2]

    val32 = t.read32(REG_OFDM0_XA_RX_IQ_IMBALANCE)
    val32 &= ~0x3ff
    val32 |= (reg & 0x3ff)
    t.write32(REG_OFDM0_XA_RX_IQ_IMBALANCE, val32)

    reg = result[3] & 0x3F
    val32 = t.read32(REG_OFDM0_XA_RX_IQ_IMBALANCE)
    val32 &= ~0xfc00
    val32 |= ((reg << 10) & 0xfc00)
    t.write32(REG_OFDM0_XA_RX_IQ_IMBALANCE, val32)

    reg = (result[3] >> 6) & 0xF
    val32 = t.read32(REG_OFDM0_RX_IQ_EXT_ANTA)
    val32 &= ~0xf0000000
    val32 |= (reg << 28)
    t.write32(REG_OFDM0_RX_IQ_EXT_ANTA, val32)


def iq_calibrate(t: RTL8188FTVTransport) -> None:
    """Mirror of `rtl8188fu_phy_iq_calibrate` (8188f.c:1218-1383).

    Trials t=0,1, simularity compare, fill matrix. Produces 378 ops.
    """
    path_sel_bb = t.read32(REG_S0S1_PATH_SWITCH)
    path_sel_rf = read_rfreg(t, 0, RF6052_REG_S0S1)

    result = [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]
    candidate = -1
    pi_enabled = False
    adda_backup = [0] * 16
    mac_backup = [0] * 4
    bb_backup = [0] * 9

    for i in range(3):
        pi_enabled = _inner_iqcalibrate(
            t, result, i, pi_enabled,
            adda_backup, mac_backup, bb_backup,
        )

        if i == 1:
            simu = _gen2_simularity_compare(result, 0, 1)
            if simu:
                candidate = 0
                break

        if i == 2:
            simu = _gen2_simularity_compare(result, 0, 2)
            if simu:
                candidate = 0
                break

            simu = _gen2_simularity_compare(result, 1, 2)
            if simu:
                candidate = 1
            else:
                reg_tmp = 0
                for j in range(8):
                    reg_tmp += result[3][j]
                if reg_tmp:
                    candidate = 3
                else:
                    candidate = -1

    if candidate >= 0:
        reg_e94 = result[candidate][0]
        reg_ea4 = result[candidate][2]

        if reg_e94:
            _fill_iqk_matrix_a(t, result[candidate], tx_only=(reg_ea4 == 0))

    _save_bb_recovery(t)

    t.write32(REG_S0S1_PATH_SWITCH, path_sel_bb)
    write_rfreg(t, 0, RF6052_REG_S0S1, path_sel_rf)


def enable_thermal_meter(t: RTL8188FTVTransport) -> None:
    """Enable thermal meter (core.c:4400-4405, 8188F branch).

    Produces 7 ops.
    """
    val32 = read_rfreg(t, 0, RF6052_REG_T_METER_8723B)
    val32 |= 0x30000
    write_rfreg(t, 0, RF6052_REG_T_METER_8723B, val32)


def init_device_phy_tail(t: RTL8188FTVTransport) -> None:
    """Post-calibration init tail (core.c:4412-4496, 8188F branch).

    NAV_UPPER write + FWHW_TXQ ack bit + CCK/CFO reads.  Produces 5 ops.
    """
    val8 = (30000 + NAV_UPPER_UNIT - 1) // NAV_UPPER_UNIT
    t.write8(REG_NAV_UPPER, val8)

    val32 = t.read32(REG_FWHW_TXQ_CTRL)
    val32 |= FWHW_TXQ_CTRL_XMIT_MGMT_ACK
    t.write32(REG_FWHW_TXQ_CTRL, val32)

    val32 = t.read32(0x0a9c)  # core.c:4489 — CCK new-AGC read, no named macro
    val32 = t.read32(REG_OFDM1_CFO_TRACKING)


def _inner_iqcalibrate(
    t: RTL8188FTVTransport, result: list[list[int]], i: int,
    pi_enabled: bool,
    adda_backup: list[int], mac_backup: list[int], bb_backup: list[int],
) -> bool:
    """One IQ calibrate trial (8188f.c:1063-1216).

    Returns the ``pi_enabled`` flag sampled at trial 0 (kernel keeps it in
    ``priv->pi_enabled`` across trials).  ``adda_backup``/``mac_backup``/``bb_backup``
    are filled only on trial 0 and restored on subsequent trials.
    """
    rx_initial_gain = t.read32(REG_OFDM0_XA_AGC_CORE1)

    if i == 0:
        for j, reg in enumerate(_ADDA_REGS):
            adda_backup[j] = t.read32(reg)
        for j in range(3):
            mac_backup[j] = t.read8(_MAC_REGS[j])
        mac_backup[3] = t.read32(_MAC_REGS[3])
        for j, reg in enumerate(_BB_REGS):
            bb_backup[j] = t.read32(reg)

    _path_adda_on(t)

    if i == 0:
        val32 = t.read32(REG_FPGA0_XA_HSSI_PARM1)
        pi_enabled = bool(val32 & FPGA0_HSSI_PARM1_PI)

    path_sel_bb = t.read32(REG_S0S1_PATH_SWITCH)
    path_sel_rf = read_rfreg(t, 0, RF6052_REG_S0S1)

    t.write32(REG_OFDM0_TRX_PATH_ENABLE, 0x03a05600)
    t.write32(REG_OFDM0_TR_MUX_PAR, 0x000800e4)
    t.write32(REG_FPGA0_XCD_RF_SW_CTRL, 0x25204000)

    val32 = t.read32(REG_TXPTCL_CTRL)
    val32 |= 0x00ff0000
    t.write32(REG_TXPTCL_CTRL, val32)

    val32 = t.read32(REG_FPGA0_IQK)
    val32 &= 0xff
    val32 |= 0x80800000
    t.write32(REG_FPGA0_IQK, val32)
    t.write32(REG_TX_IQK, 0x01007c00)
    t.write32(REG_RX_IQK, 0x01004800)

    lok_result = [0]
    path_a_ok = 0
    for _ in range(2):
        path_a_ok = _iqk_path_a(t, lok_result)
        if path_a_ok == 0x01:
            val32 = t.read32(REG_FPGA0_IQK)
            val32 &= 0xff
            t.write32(REG_FPGA0_IQK, val32)

            val32 = t.read32(REG_TX_POWER_BEFORE_IQK_A)
            result[i][0] = (val32 >> 16) & 0x3ff

            val32 = t.read32(REG_TX_POWER_AFTER_IQK_A)
            result[i][1] = (val32 >> 16) & 0x3ff
            break

    for _ in range(2):
        path_a_ok = _rx_iqk_path_a(t, lok_result[0])
        if path_a_ok == 0x03:
            val32 = t.read32(REG_RX_POWER_BEFORE_IQK_A_2)
            result[i][2] = (val32 >> 16) & 0x3ff

            val32 = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
            result[i][3] = (val32 >> 16) & 0x3ff
            break

    val32 = t.read32(REG_FPGA0_IQK)
    val32 &= 0xff
    t.write32(REG_FPGA0_IQK, val32)

    if i == 0:
        return pi_enabled

    if not pi_enabled:
        t.write32(REG_FPGA0_XA_HSSI_PARM1, 0x01000000)
        t.write32(REG_FPGA0_XB_HSSI_PARM1, 0x01000000)

    for j, reg in enumerate(_ADDA_REGS):
        t.write32(reg, adda_backup[j])
    for j in range(3):
        t.write8(_MAC_REGS[j], mac_backup[j])
    t.write32(_MAC_REGS[3], mac_backup[3])
    for j, reg in enumerate(_BB_REGS):
        t.write32(reg, bb_backup[j])

    t.write32(REG_S0S1_PATH_SWITCH, path_sel_bb)
    write_rfreg(t, 0, RF6052_REG_S0S1, path_sel_rf)

    val32 = t.read32(REG_OFDM0_XA_AGC_CORE1)
    val32 &= 0xffffff00
    val32 |= 0x50
    t.write32(REG_OFDM0_XA_AGC_CORE1, val32)
    val32 = t.read32(REG_OFDM0_XA_AGC_CORE1)
    val32 &= 0xffffff00
    val32 |= rx_initial_gain & 0xff
    t.write32(REG_OFDM0_XA_AGC_CORE1, val32)

    t.write32(REG_TX_IQK_TONE_A, 0x01008c00)
    t.write32(REG_RX_IQK_TONE_A, 0x01008c00)
    return pi_enabled


# ---- BB recovery save (core.c:3004-3008 via 8188f.c:1289) ----------


def _path_adda_on(t: RTL8188FTVTransport) -> None:
    """Turn path-A ADDA on (core.c:3045-3052)."""
    t.write32(_ADDA_REGS[0], _ADDA_1T_INIT)
    for reg in _ADDA_REGS[1:]:
        t.write32(reg, _ADDA_1T_PATH_ON)


def _save_bb_recovery(t: RTL8188FTVTransport) -> None:
    """Save IQK BB registers (8188f.c:1289 via core.c:3004)."""
    for reg in _IQK_BB_REGS:
        t.read32(reg)


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
    and sets the OFDM TX/RX path mask.  Produces 19 ops.
    """
    efuse_byte = read_efuse_byte(t, PPG_BB_GAIN_2G_TXA_OFFSET_8188F)

    if efuse_byte != 0xFF:
        bb_gain = efuse_byte & PPG_BB_GAIN_2G_TX_OFFSET_MASK
        if bb_gain == PPG_BB_GAIN_2G_TX_OFFSET_MASK:
            bb_gain = 0
        elif bb_gain & 1:
            bb_gain = bb_gain >> 1
        else:
            bb_gain = -(bb_gain >> 1)
        val8 = abs(bb_gain)
        if bb_gain > 0:
            val8 |= BIT(5)
        val32 = read_rfreg(t, 0, RF6052_REG_UNKNOWN_55)
        val32 &= ~0xFC000
        val32 |= val8 << 14
        write_rfreg(t, 0, RF6052_REG_UNKNOWN_55, val32)

    t.write8(REG_RF_CTRL, RF_ENABLE | RF_RSTB | RF_SDMRSTB)

    val32 = t.read32(REG_OFDM0_TRX_PATH_ENABLE)
    val32 &= ~(OFDM_RF_PATH_RX_MASK | OFDM_RF_PATH_TX_MASK)
    val32 |= OFDM_RF_PATH_RX_A | OFDM_RF_PATH_TX_A
    t.write32(REG_OFDM0_TRX_PATH_ENABLE, val32)

    t.write8(REG_TXPAUSE, 0x00)


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
