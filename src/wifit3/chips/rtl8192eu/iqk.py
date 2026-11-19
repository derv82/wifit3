"""Port of the RTL8192EU IQ calibration: 8192e.c rtl8192eu_*iqk* plus the core.c helpers it reaches.

phy_iq_calibrate runs three passes, picks the best by rtl8xxxu_gen2_simularity_compare, and applies
the result with rtl8xxxu_fill_iqk_matrix_{a,b}. Register sequences come from 8192e.c; the shared
matrix/compare/backup helpers from core.c (RTL8192E branch where the C differs from RTL8192F).
"""
from __future__ import annotations

import time

from . import constants as C
from .core import (
    RF_A,
    RF_B,
    Rtl8xxxuPriv,
    rtl8xxxu_read32,
    rtl8xxxu_read8,
    rtl8xxxu_write8,
    rtl8xxxu_write32,
    rtl8xxxu_write_rfreg,
)

MAX_TOLERANCE = 5  # core.c:2874

# adda_regs[RTL8XXXU_ADDA_REGS], iqk_mac_regs[RTL8XXXU_MAC_REGS], iqk_bb_regs[RTL8XXXU_BB_REGS]
# in rtl8192eu_phy_iqcalibrate order (8192e.c:1072).
ADDA_REGS = (
    C.REG_FPGA0_XCD_SWITCH_CTRL, C.REG_BLUETOOTH, C.REG_RX_WAIT_CCA, C.REG_TX_CCK_RFON,
    C.REG_TX_CCK_BBON, C.REG_TX_OFDM_RFON, C.REG_TX_OFDM_BBON, C.REG_TX_TO_RX,
    C.REG_TX_TO_TX, C.REG_RX_CCK, C.REG_RX_OFDM, C.REG_RX_WAIT_RIFS,
    C.REG_RX_TO_RX, C.REG_STANDBY, C.REG_SLEEP, C.REG_PMPD_ANAEN,
)
IQK_MAC_REGS = (C.REG_TXPAUSE, C.REG_BEACON_CTRL, C.REG_BEACON_CTRL_1, C.REG_GPIO_MUXCFG)
IQK_BB_REGS = (
    C.REG_OFDM0_TRX_PATH_ENABLE, C.REG_OFDM0_TR_MUX_PAR, C.REG_FPGA0_XCD_RF_SW_CTRL,
    C.REG_CONFIG_ANT_A, C.REG_CONFIG_ANT_B, C.REG_FPGA0_XAB_RF_SW_CTRL,
    C.REG_FPGA0_XA_RF_INT_OE, C.REG_FPGA0_XB_RF_INT_OE, C.REG_CCK0_AFE_SETTING,
)
# rtl8xxxu_iqk_phy_iq_bb_reg[RTL8XXXU_BB_REGS], core.c:642
IQK_PHY_IQ_BB_REG = (
    C.REG_OFDM0_XA_RX_IQ_IMBALANCE, C.REG_OFDM0_XB_RX_IQ_IMBALANCE, C.REG_OFDM0_ENERGY_CCA_THRES,
    C.REG_OFDM0_AGC_RSSI_TABLE, C.REG_OFDM0_XA_TX_IQ_IMBALANCE, C.REG_OFDM0_XB_TX_IQ_IMBALANCE,
    C.REG_OFDM0_XC_TX_AFE, C.REG_OFDM0_XD_TX_AFE, C.REG_OFDM0_RX_IQ_EXT_ANTA,
)


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _sign_extend_10(value: int) -> int:
    """C ``value | 0xfffffc00`` stored in an int."""
    return value - 0x400 if value & 0x00000200 else value


def rtl8xxxu_fill_iqk_matrix_a(priv: Rtl8xxxuPriv, iqk_ok: bool, result: list[list[int]],
                               candidate: int, tx_only: bool) -> None:
    """core.c:2707."""
    if not iqk_ok:
        return

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XA_TX_IQ_IMBALANCE)
    oldval = val32 >> 22

    x = result[candidate][0]
    if (x & 0x00000200) != 0:
        x = x | 0xFFFFFC00
    tx0_a = _u32(x * oldval) >> 8

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XA_TX_IQ_IMBALANCE)
    val32 &= ~0x3FF
    val32 |= tx0_a
    rtl8xxxu_write32(priv, C.REG_OFDM0_XA_TX_IQ_IMBALANCE, val32)

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_ENERGY_CCA_THRES)
    val32 &= ~C.BIT(31)
    if (_u32(x * oldval) >> 7) & 0x1:
        val32 |= C.BIT(31)
    rtl8xxxu_write32(priv, C.REG_OFDM0_ENERGY_CCA_THRES, val32)

    y = result[candidate][1]
    if (y & 0x00000200) != 0:
        y = y | 0xFFFFFC00
    tx0_c = _u32(y * oldval) >> 8

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XC_TX_AFE)
    val32 &= ~0xF0000000
    val32 |= ((tx0_c & 0x3C0) >> 6) << 28
    rtl8xxxu_write32(priv, C.REG_OFDM0_XC_TX_AFE, val32)

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XA_TX_IQ_IMBALANCE)
    val32 &= ~0x003F0000
    val32 |= (tx0_c & 0x3F) << 16
    rtl8xxxu_write32(priv, C.REG_OFDM0_XA_TX_IQ_IMBALANCE, val32)

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_ENERGY_CCA_THRES)
    val32 &= ~C.BIT(29)
    if (_u32(y * oldval) >> 7) & 0x1:
        val32 |= C.BIT(29)
    rtl8xxxu_write32(priv, C.REG_OFDM0_ENERGY_CCA_THRES, val32)

    if tx_only:
        return

    reg = result[candidate][2]
    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XA_RX_IQ_IMBALANCE)
    val32 &= ~0x3FF
    val32 |= reg & 0x3FF
    rtl8xxxu_write32(priv, C.REG_OFDM0_XA_RX_IQ_IMBALANCE, val32)

    reg = result[candidate][3] & 0x3F
    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XA_RX_IQ_IMBALANCE)
    val32 &= ~0xFC00
    val32 |= (reg << 10) & 0xFC00
    rtl8xxxu_write32(priv, C.REG_OFDM0_XA_RX_IQ_IMBALANCE, val32)

    reg = (result[candidate][3] >> 6) & 0xF
    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_RX_IQ_EXT_ANTA)
    val32 &= ~0xF0000000
    val32 |= reg << 28
    rtl8xxxu_write32(priv, C.REG_OFDM0_RX_IQ_EXT_ANTA, val32)


def rtl8xxxu_fill_iqk_matrix_b(priv: Rtl8xxxuPriv, iqk_ok: bool, result: list[list[int]],
                               candidate: int, tx_only: bool) -> None:
    """core.c:2793, RTL8192E branch (final reg via REG_OFDM0_AGC_RSSI_TABLE, not REG_RXIQB_EXT)."""
    if not iqk_ok:
        return

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XB_TX_IQ_IMBALANCE)
    oldval = val32 >> 22

    x = result[candidate][4]
    if (x & 0x00000200) != 0:
        x = x | 0xFFFFFC00
    tx1_a = _u32(x * oldval) >> 8

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XB_TX_IQ_IMBALANCE)
    val32 &= ~0x3FF
    val32 |= tx1_a
    rtl8xxxu_write32(priv, C.REG_OFDM0_XB_TX_IQ_IMBALANCE, val32)

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_ENERGY_CCA_THRES)
    val32 &= ~C.BIT(27)
    if (_u32(x * oldval) >> 7) & 0x1:
        val32 |= C.BIT(27)
    rtl8xxxu_write32(priv, C.REG_OFDM0_ENERGY_CCA_THRES, val32)

    y = result[candidate][5]
    if (y & 0x00000200) != 0:
        y = y | 0xFFFFFC00
    tx1_c = _u32(y * oldval) >> 8

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XD_TX_AFE)
    val32 &= ~0xF0000000
    val32 |= ((tx1_c & 0x3C0) >> 6) << 28
    rtl8xxxu_write32(priv, C.REG_OFDM0_XD_TX_AFE, val32)

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XB_TX_IQ_IMBALANCE)
    val32 &= ~0x003F0000
    val32 |= (tx1_c & 0x3F) << 16
    rtl8xxxu_write32(priv, C.REG_OFDM0_XB_TX_IQ_IMBALANCE, val32)

    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_ENERGY_CCA_THRES)
    val32 &= ~C.BIT(25)
    if (_u32(y * oldval) >> 7) & 0x1:
        val32 |= C.BIT(25)
    rtl8xxxu_write32(priv, C.REG_OFDM0_ENERGY_CCA_THRES, val32)

    if tx_only:
        return

    reg = result[candidate][6]
    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XB_RX_IQ_IMBALANCE)
    val32 &= ~0x3FF
    val32 |= reg & 0x3FF
    rtl8xxxu_write32(priv, C.REG_OFDM0_XB_RX_IQ_IMBALANCE, val32)

    reg = result[candidate][7] & 0x3F
    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XB_RX_IQ_IMBALANCE)
    val32 &= ~0xFC00
    val32 |= (reg << 10) & 0xFC00
    rtl8xxxu_write32(priv, C.REG_OFDM0_XB_RX_IQ_IMBALANCE, val32)

    reg = (result[candidate][7] >> 6) & 0xF
    val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_AGC_RSSI_TABLE)
    val32 &= ~0x0000F000
    val32 |= reg << 12
    rtl8xxxu_write32(priv, C.REG_OFDM0_AGC_RSSI_TABLE, val32)


def rtl8xxxu_gen2_simularity_compare(priv: Rtl8xxxuPriv, result: list[list[int]], c1: int, c2: int) -> bool:
    """core.c:2890."""
    candidate = [-1, -1]
    retval = True
    bound = 8 if priv.tx_paths > 1 else 4

    simubitmap = 0
    for i in range(bound):
        if i & 1:
            tmp1 = _sign_extend_10(result[c1][i])
            tmp2 = _sign_extend_10(result[c2][i])
        else:
            tmp1 = result[c1][i]
            tmp2 = result[c2][i]

        diff = tmp1 - tmp2 if tmp1 > tmp2 else tmp2 - tmp1

        if diff > MAX_TOLERANCE:
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
                retval = False
        return retval

    if not simubitmap & 0x03:
        for i in range(0, 2):
            result[3][i] = result[c1][i]
    if not simubitmap & 0x0C:
        for i in range(2, 4):
            result[3][i] = result[c1][i]
    if not simubitmap & 0x30 and priv.tx_paths > 1:
        for i in range(4, 6):
            result[3][i] = result[c1][i]
    if not simubitmap & 0xC0 and priv.tx_paths > 1:
        for i in range(6, 8):
            result[3][i] = result[c1][i]
    return False


def rtl8xxxu_save_mac_regs(priv: Rtl8xxxuPriv, reg: tuple[int, ...], backup: list[int]) -> None:
    """core.c:2652."""
    backup.clear()
    for i in range(C.RTL8XXXU_MAC_REGS - 1):
        backup.append(rtl8xxxu_read8(priv, reg[i]))
    backup.append(rtl8xxxu_read32(priv, reg[C.RTL8XXXU_MAC_REGS - 1]))


def rtl8xxxu_restore_mac_regs(priv: Rtl8xxxuPriv, reg: tuple[int, ...], backup: list[int]) -> None:
    """core.c:2665."""
    for i in range(C.RTL8XXXU_MAC_REGS - 1):
        rtl8xxxu_write8(priv, reg[i], backup[i])
    rtl8xxxu_write32(priv, reg[C.RTL8XXXU_MAC_REGS - 1], backup[C.RTL8XXXU_MAC_REGS - 1])


def rtl8xxxu_save_regs(priv: Rtl8xxxuPriv, regs: tuple[int, ...], backup: list[int], count: int) -> None:
    """core.c:2637."""
    backup.clear()
    for i in range(count):
        backup.append(rtl8xxxu_read32(priv, regs[i]))


def rtl8xxxu_restore_regs(priv: Rtl8xxxuPriv, regs: tuple[int, ...], backup: list[int], count: int) -> None:
    """core.c:2646."""
    for i in range(count):
        rtl8xxxu_write32(priv, regs[i], backup[i])


def rtl8xxxu_path_adda_on(priv: Rtl8xxxuPriv, regs: tuple[int, ...], path_a_on: bool) -> None:
    """core.c:2676."""
    if priv.tx_paths == 1:
        path_on = priv.fops.adda_1t_path_on
        rtl8xxxu_write32(priv, regs[0], priv.fops.adda_1t_init)
    else:
        path_on = priv.fops.adda_2t_path_on_a if path_a_on else priv.fops.adda_2t_path_on_b
        rtl8xxxu_write32(priv, regs[0], path_on)

    for i in range(1, C.RTL8XXXU_ADDA_REGS):
        rtl8xxxu_write32(priv, regs[i], path_on)


def rtl8xxxu_mac_calibration(priv: Rtl8xxxuPriv, regs: tuple[int, ...], backup: list[int]) -> None:
    """core.c:2694."""
    rtl8xxxu_write8(priv, regs[0], 0x3f)
    for i in range(1, C.RTL8XXXU_MAC_REGS - 1):
        rtl8xxxu_write8(priv, regs[i], backup[i] & ~C.BIT(3))
    rtl8xxxu_write8(priv, regs[C.RTL8XXXU_MAC_REGS - 1], backup[C.RTL8XXXU_MAC_REGS - 1] & ~C.BIT(5))


def rtl8192eu_iqk_path_a(priv: Rtl8xxxuPriv) -> int:
    """8192e.c:709. TX IQK path A; PA/PAD controlled by 0x0."""
    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00000000)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_GAIN_CCA, 0x00180)

    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_WE_LUT, 0x800a0)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_RCK_OS, 0x20000)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_TXPA_G1, 0x0000f)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_TXPA_G2, 0x07f77)

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x80800000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x18008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x38008c1c)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_A, 0x82140303)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_A, 0x68160000)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x00462911)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xf9000000)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xf8000000)

    time.sleep(10e-3)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_A)

    result = 0
    if not (reg_eac & C.BIT(28)) and (reg_e94 & 0x03ff0000) != 0x01420000 and \
            (reg_e9c & 0x03ff0000) != 0x00420000:
        result |= 0x01
    return result


def rtl8192eu_rx_iqk_path_a(priv: Rtl8xxxuPriv) -> int:
    """8192e.c:759. RX IQK path A."""
    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00)

    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_WE_LUT, 0x800a0)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_RCK_OS, 0x30000)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_TXPA_G1, 0x0000f)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_TXPA_G2, 0xf1173)

    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_WE_LUT, 0x800a0)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_RCK_OS, 0x30000)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_TXPA_G1, 0x0000f)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_TXPA_G2, 0xf1173)

    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_GAIN_CCA, 0x00980)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_PAD_TXG, 0x511e0)

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x80800000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK, 0x01007c00)
    rtl8xxxu_write32(priv, C.REG_RX_IQK, 0x01004800)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x18008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x38008c1c)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_A, 0x8216031f)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_A, 0x6816031f)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x0046a911)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xf9000000)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xf8000000)

    time.sleep(10e-3)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_A)

    result = 0
    if not (reg_eac & C.BIT(28)) and (reg_e94 & 0x03ff0000) != 0x01420000 and \
            (reg_e9c & 0x03ff0000) != 0x00420000:
        result |= 0x01
    else:
        rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00000000)
        rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_GAIN_CCA, 0x180)
        return result

    val32 = 0x80007c00 | (reg_e94 & 0x03ff0000) | ((reg_e9c >> 16) & 0x03ff)
    rtl8xxxu_write32(priv, C.REG_TX_IQK, val32)

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00000000)

    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_WE_LUT, 0x800a0)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_RCK_OS, 0x30000)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_TXPA_G1, 0x0000f)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_TXPA_G2, 0xf7ff2)

    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_WE_LUT, 0x800a0)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_RCK_OS, 0x30000)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_TXPA_G1, 0x0000f)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_TXPA_G2, 0xf7ff2)

    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_GAIN_CCA, 0x00980)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_PAD_TXG, 0x510e0)

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x80800000)

    rtl8xxxu_write32(priv, C.REG_RX_IQK, 0x01004800)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x18008c1c)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x38008c1c)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_A, 0x821608ff)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_A, 0x281608ff)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x0046a891)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xf9000000)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xf8000000)

    time.sleep(10e-3)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_ea4 = rtl8xxxu_read32(priv, C.REG_RX_POWER_BEFORE_IQK_A_2)

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00000000)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_GAIN_CCA, 0x180)

    if not (reg_eac & C.BIT(27)) and (reg_ea4 & 0x03ff0000) != 0x01320000 and \
            (reg_eac & 0x03ff0000) != 0x00360000:
        result |= 0x02
    return result


def rtl8192eu_iqk_path_b(priv: Rtl8xxxuPriv) -> int:
    """8192e.c:886. TX IQK path B."""
    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00000000)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_GAIN_CCA, 0x00180)

    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_WE_LUT, 0x800a0)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_RCK_OS, 0x20000)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_TXPA_G1, 0x0000f)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_TXPA_G2, 0x07f77)

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x80800000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x18008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x38008c1c)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_B, 0x82140303)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_B, 0x68160000)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x00462911)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xfa000000)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xf8000000)

    time.sleep(1e-3)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_eb4 = rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_B)
    reg_ebc = rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_B)

    result = 0
    if not (reg_eac & C.BIT(31)) and (reg_eb4 & 0x03ff0000) != 0x01420000 and \
            (reg_ebc & 0x03ff0000) != 0x00420000:
        result |= 0x01
    return result


def rtl8192eu_rx_iqk_path_b(priv: Rtl8xxxuPriv) -> int:
    """8192e.c:935. RX IQK path B."""
    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00000000)

    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_WE_LUT, 0x800a0)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_RCK_OS, 0x30000)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_TXPA_G1, 0x0000f)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_TXPA_G2, 0xf1173)

    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_WE_LUT, 0x800a0)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_RCK_OS, 0x30000)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_TXPA_G1, 0x0000f)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_TXPA_G2, 0xf1173)

    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_GAIN_CCA, 0x00980)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_PAD_TXG, 0x511e0)

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x80800000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK, 0x01007c00)
    rtl8xxxu_write32(priv, C.REG_RX_IQK, 0x01004800)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x18008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x38008c1c)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_B, 0x8216031f)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_B, 0x6816031f)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x0046a911)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xfa000000)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xf8000000)

    time.sleep(10e-3)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_eb4 = rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_B)
    reg_ebc = rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_B)

    result = 0
    if not (reg_eac & C.BIT(31)) and (reg_eb4 & 0x03ff0000) != 0x01420000 and \
            (reg_ebc & 0x03ff0000) != 0x00420000:
        result |= 0x01
    else:
        # Vendor driver restores RF_A here which the C comments is a bug; follow the C and use RF_B.
        rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00000000)
        rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_GAIN_CCA, 0x180)
        return result

    val32 = 0x80007c00 | (reg_eb4 & 0x03ff0000) | ((reg_ebc >> 16) & 0x03ff)
    rtl8xxxu_write32(priv, C.REG_TX_IQK, val32)

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00000000)

    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_WE_LUT, 0x800a0)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_RCK_OS, 0x30000)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_TXPA_G1, 0x0000f)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_TXPA_G2, 0xf7ff2)

    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_WE_LUT, 0x800a0)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_RCK_OS, 0x30000)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_TXPA_G1, 0x0000f)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_TXPA_G2, 0xf7ff2)

    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_GAIN_CCA, 0x00980)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_PAD_TXG, 0x510e0)

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x80800000)

    rtl8xxxu_write32(priv, C.REG_RX_IQK, 0x01004800)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x38008c1c)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x18008c1c)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_A, 0x821608ff)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_A, 0x281608ff)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x0046a891)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xfa000000)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xf8000000)

    time.sleep(10e-3)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_ec4 = rtl8xxxu_read32(priv, C.REG_RX_POWER_BEFORE_IQK_B_2)
    reg_ecc = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_B_2)

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00000000)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_GAIN_CCA, 0x180)

    if not (reg_eac & C.BIT(30)) and (reg_ec4 & 0x03ff0000) != 0x01320000 and \
            (reg_ecc & 0x03ff0000) != 0x00360000:
        result |= 0x02
    return result


def rtl8192eu_phy_iqcalibrate(priv: Rtl8xxxuPriv, result: list[list[int]], t: int) -> None:
    """8192e.c:1066."""
    retry = 2
    xa_agc = rtl8xxxu_read32(priv, C.REG_OFDM0_XA_AGC_CORE1) & 0xff
    xb_agc = rtl8xxxu_read32(priv, C.REG_OFDM0_XB_AGC_CORE1) & 0xff

    if t == 0:
        rtl8xxxu_save_regs(priv, ADDA_REGS, priv.adda_backup, C.RTL8XXXU_ADDA_REGS)
        rtl8xxxu_save_mac_regs(priv, IQK_MAC_REGS, priv.mac_backup)
        rtl8xxxu_save_regs(priv, IQK_BB_REGS, priv.bb_backup, C.RTL8XXXU_BB_REGS)

    rtl8xxxu_path_adda_on(priv, ADDA_REGS, True)

    rtl8xxxu_mac_calibration(priv, IQK_MAC_REGS, priv.mac_backup)

    val32 = rtl8xxxu_read32(priv, C.REG_CCK0_AFE_SETTING)
    val32 |= 0x0f000000
    rtl8xxxu_write32(priv, C.REG_CCK0_AFE_SETTING, val32)

    rtl8xxxu_write32(priv, C.REG_OFDM0_TRX_PATH_ENABLE, 0x03a05600)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TR_MUX_PAR, 0x000800e4)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XCD_RF_SW_CTRL, 0x22208200)

    val32 = rtl8xxxu_read32(priv, C.REG_FPGA0_XAB_RF_SW_CTRL)
    val32 |= C.FPGA0_RF_PAPE | (C.FPGA0_RF_PAPE << C.FPGA0_RF_BD_CTRL_SHIFT)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XAB_RF_SW_CTRL, val32)

    val32 = rtl8xxxu_read32(priv, C.REG_FPGA0_XA_RF_INT_OE)
    val32 |= C.BIT(10)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XA_RF_INT_OE, val32)
    val32 = rtl8xxxu_read32(priv, C.REG_FPGA0_XB_RF_INT_OE)
    val32 |= C.BIT(10)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XB_RF_INT_OE, val32)

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x80800000)
    rtl8xxxu_write32(priv, C.REG_TX_IQK, 0x01007c00)
    rtl8xxxu_write32(priv, C.REG_RX_IQK, 0x01004800)

    path_a_ok = 0
    for _ in range(retry):
        path_a_ok = rtl8192eu_iqk_path_a(priv)
        if path_a_ok == 0x01:
            result[t][0] = (rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_A) >> 16) & 0x3ff
            result[t][1] = (rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_A) >> 16) & 0x3ff
            break

    for _ in range(retry):
        path_a_ok = rtl8192eu_rx_iqk_path_a(priv)
        if path_a_ok == 0x03:
            result[t][2] = (rtl8xxxu_read32(priv, C.REG_RX_POWER_BEFORE_IQK_A_2) >> 16) & 0x3ff
            result[t][3] = (rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2) >> 16) & 0x3ff
            break

    if priv.rf_paths > 1:
        rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00000000)
        rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_AC, 0x10000)
        rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x80800000)

        rtl8xxxu_path_adda_on(priv, ADDA_REGS, False)

        rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x80800000)
        rtl8xxxu_write32(priv, C.REG_TX_IQK, 0x01007c00)
        rtl8xxxu_write32(priv, C.REG_RX_IQK, 0x01004800)

        path_b_ok = 0
        for _ in range(retry):
            path_b_ok = rtl8192eu_iqk_path_b(priv)
            if path_b_ok == 0x01:
                result[t][4] = (rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_B) >> 16) & 0x3ff
                result[t][5] = (rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_B) >> 16) & 0x3ff
                break

        for _ in range(retry):
            path_b_ok = rtl8192eu_rx_iqk_path_b(priv)
            if path_b_ok == 0x03:
                result[t][6] = (rtl8xxxu_read32(priv, C.REG_RX_POWER_BEFORE_IQK_B_2) >> 16) & 0x3ff
                result[t][7] = (rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_B_2) >> 16) & 0x3ff
                break

    rtl8xxxu_write32(priv, C.REG_FPGA0_IQK, 0x00000000)

    if t:
        rtl8xxxu_restore_regs(priv, ADDA_REGS, priv.adda_backup, C.RTL8XXXU_ADDA_REGS)
        rtl8xxxu_restore_mac_regs(priv, IQK_MAC_REGS, priv.mac_backup)
        rtl8xxxu_restore_regs(priv, IQK_BB_REGS, priv.bb_backup, C.RTL8XXXU_BB_REGS)

        val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XA_AGC_CORE1) & 0xffffff00
        rtl8xxxu_write32(priv, C.REG_OFDM0_XA_AGC_CORE1, val32 | 0x50)
        rtl8xxxu_write32(priv, C.REG_OFDM0_XA_AGC_CORE1, val32 | xa_agc)

        if priv.rf_paths > 1:
            val32 = rtl8xxxu_read32(priv, C.REG_OFDM0_XB_AGC_CORE1) & 0xffffff00
            rtl8xxxu_write32(priv, C.REG_OFDM0_XB_AGC_CORE1, val32 | 0x50)
            rtl8xxxu_write32(priv, C.REG_OFDM0_XB_AGC_CORE1, val32 | xb_agc)

        rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x01008c00)
        rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x01008c00)


def rtl8192eu_phy_iq_calibrate(priv: Rtl8xxxuPriv) -> None:
    """8192e.c:1252. Three passes, pick the best, apply with fill_iqk_matrix_{a,b}."""
    result = [[0] * 8 for _ in range(4)]
    candidate = -1
    path_a_ok = False
    path_b_ok = False

    for i in range(3):
        rtl8192eu_phy_iqcalibrate(priv, result, i)

        if i == 1:
            if rtl8xxxu_gen2_simularity_compare(priv, result, 0, 1):
                candidate = 0
                break

        if i == 2:
            if rtl8xxxu_gen2_simularity_compare(priv, result, 0, 2):
                candidate = 0
                break
            if rtl8xxxu_gen2_simularity_compare(priv, result, 1, 2):
                candidate = 1
            else:
                candidate = 3

    if candidate >= 0:
        reg_e94 = result[candidate][0]
        priv.rege94 = reg_e94
        reg_e9c = result[candidate][1]
        priv.rege9c = reg_e9c
        reg_ea4 = result[candidate][2]
        reg_eb4 = result[candidate][4]
        priv.regeb4 = reg_eb4
        reg_ebc = result[candidate][5]
        priv.regebc = reg_ebc
        reg_ec4 = result[candidate][6]
        path_a_ok = True
        path_b_ok = True
    else:
        reg_e94 = reg_eb4 = priv.rege94 = priv.regeb4 = 0x100
        reg_e9c = reg_ebc = priv.rege9c = priv.regebc = 0x0
        reg_ea4 = reg_ec4 = 0

    if reg_e94 and candidate >= 0:
        rtl8xxxu_fill_iqk_matrix_a(priv, path_a_ok, result, candidate, reg_ea4 == 0)

    if priv.rf_paths > 1:
        rtl8xxxu_fill_iqk_matrix_b(priv, path_b_ok, result, candidate, reg_ec4 == 0)

    rtl8xxxu_save_regs(priv, IQK_PHY_IQ_BB_REG, priv.bb_recovery_backup, C.RTL8XXXU_BB_REGS)
