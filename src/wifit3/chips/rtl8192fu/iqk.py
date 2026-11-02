"""Port of the RTL8192FU IQ calibration: 8192f.c rtl8192fu_*iqk* plus the core.c helpers it reaches."""
from __future__ import annotations

import time

from . import constants as C
from .core import (
    RF_A,
    RF_B,
    Rtl8xxxuPriv,
    rtl8xxxu_read8,
    rtl8xxxu_read32,
    rtl8xxxu_read_rfreg,
    rtl8xxxu_write8,
    rtl8xxxu_write8_clear,
    rtl8xxxu_write32,
    rtl8xxxu_write32_clear,
    rtl8xxxu_write32_mask,
    rtl8xxxu_write32_set,
    rtl8xxxu_write_rfreg,
    rtl8xxxu_write_rfreg_mask,
    u32_get_bits,
)

MAX_TOLERANCE = 5  # core.c:2874


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def rtl8xxxu_fill_iqk_matrix_a(priv: Rtl8xxxuPriv, iqk_ok: bool, result: list[list[int]],
                               candidate: int, tx_only: bool) -> None:
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
    rtl8xxxu_write32_mask(priv, C.REG_RXIQB_EXT, 0x000000F0, reg)  # RTL8192F branch, core.c:2864


def _sign_extend_10(value: int) -> int:
    """C ``value | 0xfffffc00`` stored in an int."""
    return value - 0x400 if value & 0x00000200 else value


def rtl8xxxu_gen2_simularity_compare(priv: Rtl8xxxuPriv, result: list[list[int]], c1: int, c2: int) -> bool:
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
    backup.clear()
    for i in range(C.RTL8XXXU_MAC_REGS - 1):
        backup.append(rtl8xxxu_read8(priv, reg[i]))
    backup.append(rtl8xxxu_read32(priv, reg[C.RTL8XXXU_MAC_REGS - 1]))


def rtl8xxxu_restore_mac_regs(priv: Rtl8xxxuPriv, reg: tuple[int, ...], backup: list[int]) -> None:
    for i in range(C.RTL8XXXU_MAC_REGS - 1):
        rtl8xxxu_write8(priv, reg[i], backup[i])
    rtl8xxxu_write32(priv, reg[C.RTL8XXXU_MAC_REGS - 1], backup[C.RTL8XXXU_MAC_REGS - 1])


def rtl8xxxu_save_regs(priv: Rtl8xxxuPriv, regs: tuple[int, ...], backup: list[int], count: int) -> None:
    backup.clear()
    for i in range(count):
        backup.append(rtl8xxxu_read32(priv, regs[i]))


def rtl8xxxu_restore_regs(priv: Rtl8xxxuPriv, regs: tuple[int, ...], backup: list[int], count: int) -> None:
    for i in range(count):
        rtl8xxxu_write32(priv, regs[i], backup[i])


def _is_epa(rfe: int) -> bool:
    return rfe in (7, 8, 9, 12)


def _wait_iqk_report(priv: Rtl8xxxuPriv, reg: int) -> None:
    time.sleep(15e-3)
    ktime = 0
    while rtl8xxxu_read32(priv, reg) == 0 and ktime < 21:
        time.sleep(5e-3)
        ktime += 5


def rtl8192fu_iqk_path_a(priv: Rtl8xxxuPriv) -> int:
    rfe = priv.rfe_type
    result = 0

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)

    rtl8xxxu_write32(priv, C.REG_FPGA0_ANALOG4, 0xCCF000C0)
    rtl8xxxu_write32(priv, C.REG_ANAPWR1, 0x44FFBB44)
    rtl8xxxu_write32(priv, C.REG_RX_WAIT_CCA, 0x00400040)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TRX_PATH_ENABLE, 0x6F005403)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TR_MUX_PAR, 0x000804E4)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XCD_RF_SW_CTRL, 0x04203400)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XA_HSSI_PARM1, 0x01000100)

    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_GAIN_CCA, C.BIT(4), 1)
    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_GAIN_CCA, C.BIT(11), 1)
    val32 = 0x30 if _is_epa(rfe) else 0xE9
    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_PAD_TXG, 0x003FF, val32)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x18008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x38008C1C)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_A, 0x8214000F)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_A, 0x28140000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK, 0x01007C00)
    rtl8xxxu_write32(priv, C.REG_RX_IQK, 0x01004800)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x00E62911)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xFA005800)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xF8005800)

    _wait_iqk_report(priv, C.REG_IQK_RPT_TXA)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_A)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)

    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_WE_LUT, C.BIT(4), 1)

    val32 = rtl8xxxu_read_rfreg(priv, RF_A, C.RF6052_REG_TXMOD)
    rf_0x58_i = u32_get_bits(val32, 0xFC000)
    rf_0x58_q = u32_get_bits(val32, 0x003F0)

    for i in range(8):
        rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_TXPA_G3, 0x1C000, i)
        rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_TXPA_G3, 0x00FC0, rf_0x58_i)
        rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_TXPA_G3, 0x0003F, rf_0x58_q)

    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_AC, C.BIT(14), 0)
    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_WE_LUT, C.BIT(4), 0)
    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_GAIN_CCA, 0x00810, 0)

    if not (reg_eac & C.BIT(28)) and \
            (reg_e94 & 0x03FF0000) != 0x01420000 and \
            (reg_e9c & 0x03FF0000) != 0x00420000:
        result |= 0x01

    return result


def rtl8192fu_rx_iqk_path_a(priv: Rtl8xxxuPriv) -> int:
    result = 0

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)

    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_GAIN_CCA, C.BIT(1), 1)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_GAIN_P1, 0)
    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_GAIN_CCA, C.BIT(11), 1)
    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_PAD_TXG, 0x003FF, 0x27)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x18008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x38008C1C)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_A, 0x82160027)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_A, 0x28160000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK, 0x01007C00)
    rtl8xxxu_write32(priv, C.REG_RX_IQK, 0x01004800)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x0086A911)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xFA005800)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xF8005800)

    _wait_iqk_report(priv, C.REG_IQK_RPT_TXA)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_A)

    if not (reg_eac & C.BIT(28)) and \
            (reg_e94 & 0x03FF0000) != 0x01420000 and \
            (reg_e9c & 0x03FF0000) != 0x00420000:
        result |= 0x01
    else:
        rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)
        rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_GAIN_CCA, C.BIT(11), 0)
        return result

    val32 = 0x80007C00 | (reg_e94 & 0x3FF0000) | ((reg_e9c & 0x3FF0000) >> 16)
    rtl8xxxu_write32(priv, C.REG_TX_IQK, val32)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)

    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_GAIN_CCA, C.BIT(1), 1)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_GAIN_P1, 0)
    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_GAIN_CCA, C.BIT(11), 1)
    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_PAD_TXG, 0x003FF, 0x1E0)

    rtl8xxxu_write32(priv, C.REG_FPGA0_ANALOG4, 0xCCF000C0)
    rtl8xxxu_write32(priv, C.REG_ANAPWR1, 0x44FFBB44)
    rtl8xxxu_write32(priv, C.REG_RX_WAIT_CCA, 0x00400040)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TRX_PATH_ENABLE, 0x6F005403)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TR_MUX_PAR, 0x000804E4)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XCD_RF_SW_CTRL, 0x04203400)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XA_HSSI_PARM1, 0x01000100)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x18008C1C)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x38008C1C)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_A, 0x82170000)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_A, 0x28170000)

    rtl8xxxu_write32(priv, C.REG_RX_IQK, 0x01004800)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x0046A8D1)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xFA005800)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xF8005800)

    _wait_iqk_report(priv, C.REG_IQK_RPT_RXA)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_ea4 = rtl8xxxu_read32(priv, C.REG_RX_POWER_BEFORE_IQK_A_2)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)

    rtl8xxxu_write_rfreg_mask(priv, RF_A, C.RF6052_REG_GAIN_CCA, C.BIT(11), 0)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_GAIN_P1, 0x02000)

    if not (reg_eac & C.BIT(27)) and \
            (reg_ea4 & 0x03FF0000) != 0x01320000 and \
            (reg_eac & 0x03FF0000) != 0x00360000:
        result |= 0x02

    return result


def rtl8192fu_iqk_path_b(priv: Rtl8xxxuPriv) -> int:
    rfe = priv.rfe_type
    result = 0

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)

    rtl8xxxu_write32(priv, C.REG_FPGA0_ANALOG4, 0xCCF000C0)
    rtl8xxxu_write32(priv, C.REG_ANAPWR1, 0x44FFBB44)
    rtl8xxxu_write32(priv, C.REG_RX_WAIT_CCA, 0x00400040)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TRX_PATH_ENABLE, 0x6F005403)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TR_MUX_PAR, 0x000804E4)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XCD_RF_SW_CTRL, 0x04203400)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XA_HSSI_PARM1, 0x01000000)

    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_GAIN_CCA, C.BIT(4), 1)
    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_GAIN_CCA, C.BIT(11), 1)
    if _is_epa(rfe):
        rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_PAD_TXG, 0x003FF, 0x30)
    else:
        rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_PAD_TXG, 0x00FFF, 0xE9)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x18008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x38008C1C)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_B, 0x8214000F)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_B, 0x28140000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK, 0x01007C00)
    rtl8xxxu_write32(priv, C.REG_RX_IQK, 0x01004800)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x00E62911)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xFA005800)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xF8005800)

    _wait_iqk_report(priv, C.REG_IQK_RPT_TXB)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_eb4 = rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_B)
    reg_ebc = rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_B)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)

    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_WE_LUT, C.BIT(4), 1)

    val32 = rtl8xxxu_read_rfreg(priv, RF_B, C.RF6052_REG_TXMOD)
    rf_0x58_i = u32_get_bits(val32, 0xFC000)
    rf_0x58_q = u32_get_bits(val32, 0x003F0)

    for i in range(8):
        rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_TXPA_G3, 0x1C000, i)
        rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_TXPA_G3, 0x00FC0, rf_0x58_i)
        rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_TXPA_G3, 0x0003F, rf_0x58_q)

    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_AC, C.BIT(14), 0)
    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_WE_LUT, C.BIT(4), 0)
    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_GAIN_CCA, 0x00810, 0)

    if not (reg_eac & C.BIT(31)) and \
            (reg_eb4 & 0x03FF0000) != 0x01420000 and \
            (reg_ebc & 0x03FF0000) != 0x00420000:
        result |= 0x01

    return result


def rtl8192fu_rx_iqk_path_b(priv: Rtl8xxxuPriv) -> int:
    result = 0

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)

    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_GAIN_CCA, C.BIT(1), 1)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_GAIN_P1, 0)
    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_GAIN_CCA, C.BIT(11), 1)
    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_PAD_TXG, 0x003FF, 0x67)

    rtl8xxxu_write32(priv, C.REG_FPGA0_ANALOG4, 0xCCF000C0)
    rtl8xxxu_write32(priv, C.REG_ANAPWR1, 0x44FFBB44)
    rtl8xxxu_write32(priv, C.REG_RX_WAIT_CCA, 0x00400040)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TRX_PATH_ENABLE, 0x6F005403)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TR_MUX_PAR, 0x000804E4)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XCD_RF_SW_CTRL, 0x04203400)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XA_HSSI_PARM1, 0x01000000)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x18008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x38008C1C)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_B, 0x82160027)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_B, 0x28160000)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x0086A911)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xFA005800)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xF8005800)

    _wait_iqk_report(priv, C.REG_IQK_RPT_TXB)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_eb4 = rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_B)
    reg_ebc = rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_B)

    if not (reg_eac & C.BIT(31)) and \
            (reg_eb4 & 0x03FF0000) != 0x01420000 and \
            (reg_ebc & 0x03FF0000) != 0x00420000:
        result |= 0x01
    else:
        rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)
        rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_GAIN_CCA, C.BIT(11), 0)
        return result

    val32 = 0x80007C00 | (reg_eb4 & 0x03FF0000) | ((reg_ebc >> 16) & 0x03FF)
    rtl8xxxu_write32(priv, C.REG_TX_IQK, val32)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)

    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_GAIN_CCA, C.BIT(1), 1)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_GAIN_P1, 0)
    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_GAIN_CCA, C.BIT(11), 1)
    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_PAD_TXG, 0x003FF, 0x1E0)

    rtl8xxxu_write32(priv, C.REG_FPGA0_ANALOG4, 0xCCF000C0)
    rtl8xxxu_write32(priv, C.REG_ANAPWR1, 0x44FFBB44)
    rtl8xxxu_write32(priv, C.REG_RX_WAIT_CCA, 0x00400040)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TRX_PATH_ENABLE, 0x6F005403)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TR_MUX_PAR, 0x000804E4)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XCD_RF_SW_CTRL, 0x04203400)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XA_HSSI_PARM1, 0x01000000)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_A, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_A, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_TX_IQK_TONE_B, 0x38008C1C)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_TONE_B, 0x18008C1C)

    rtl8xxxu_write32(priv, C.REG_TX_IQK_PI_B, 0x82170000)
    rtl8xxxu_write32(priv, C.REG_RX_IQK_PI_B, 0x28170000)

    rtl8xxxu_write32(priv, C.REG_RX_IQK, 0x01004800)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_RSP, 0x0046A911)

    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xFA005800)
    rtl8xxxu_write32(priv, C.REG_IQK_AGC_PTS, 0xF8005800)

    _wait_iqk_report(priv, C.REG_IQK_RPT_RXB)

    reg_eac = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
    reg_ec4 = rtl8xxxu_read32(priv, C.REG_RX_POWER_BEFORE_IQK_B_2)
    reg_ecc = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_B_2)

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XA_HSSI_PARM1, 0x01000100)

    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_GAIN_CCA, C.BIT(11), 0)
    rtl8xxxu_write_rfreg_mask(priv, RF_B, C.RF6052_REG_GAIN_CCA, C.BIT(1), 0)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_GAIN_P1, 0x02000)

    if not (reg_eac & C.BIT(30)) and \
            (reg_ec4 & 0x03FF0000) != 0x01320000 and \
            (reg_ecc & 0x03FF0000) != 0x00360000:
        result |= 0x02

    return result


ADDA_REGS = (C.REG_ANAPWR1, C.REG_RX_WAIT_CCA)
IQK_MAC_REGS = (C.REG_TXPAUSE, C.REG_BEACON_CTRL, C.REG_BEACON_CTRL_1, C.REG_GPIO_MUXCFG)
IQK_BB_REGS = (
    C.REG_OFDM0_TRX_PATH_ENABLE, C.REG_OFDM0_TR_MUX_PAR,
    C.REG_FPGA0_XCD_RF_SW_CTRL, C.REG_CONFIG_ANT_A, C.REG_CONFIG_ANT_B,
    C.REG_DPDT_CTRL, C.REG_RFE_CTRL_ANTA_SRC,
    C.REG_RFE_CTRL_ANT_SRC2, C.REG_CCK0_AFE_SETTING,
)


def rtl8192fu_phy_iqcalibrate(priv: Rtl8xxxuPriv, result: list[list[int]], t: int) -> None:
    rfe = priv.rfe_type
    retry = 2
    path_a_ok = 0
    path_b_ok = 0

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)

    rx_initial_gain_a = rtl8xxxu_read32(priv, C.REG_OFDM0_XA_AGC_CORE1)
    rx_initial_gain_b = rtl8xxxu_read32(priv, C.REG_OFDM0_XB_AGC_CORE1)

    if t == 0:
        rtl8xxxu_save_regs(priv, ADDA_REGS, priv.adda_backup, len(ADDA_REGS))
        rtl8xxxu_save_mac_regs(priv, IQK_MAC_REGS, priv.mac_backup)
        rtl8xxxu_save_regs(priv, IQK_BB_REGS, priv.bb_backup, C.RTL8XXXU_BB_REGS)

    rtl8xxxu_write32_set(priv, C.REG_FPGA0_XCD_RF_PARM, C.BIT(31))

    rtl8xxxu_write8(priv, C.REG_TXPAUSE, 0xFF)
    rtl8xxxu_write8_clear(priv, C.REG_GPIO_MUXCFG, C.GPIO_MUXCFG_IO_SEL_ENBT)

    if _is_epa(rfe):
        # TODO: verify, untested here, needs an ePA board (rfe_type 7/8/9/12), 8192f.c:1304
        rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANTA_SRC, 0xF, 0x7)
        rtl8xxxu_write32_mask(priv, C.REG_DPDT_CTRL, 0x1, 0x0)

        rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANTA_SRC, 0xF00, 0x7)
        rtl8xxxu_write32_mask(priv, C.REG_DPDT_CTRL, 0x4, 0x0)

        rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANTA_SRC, 0xF000, 0x7)
        rtl8xxxu_write32_mask(priv, C.REG_DPDT_CTRL, 0x8, 0x0)

        rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANT_SRC2, 0xF0, 0x7)
        rtl8xxxu_write32_mask(priv, C.REG_DPDT_CTRL, 0x20000, 0x0)

        rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANT_SRC2, 0xF0000, 0x7)
        rtl8xxxu_write32_mask(priv, C.REG_DPDT_CTRL, 0x100000, 0x0)

        rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANT_SRC3, 0xF000, 0x7)
        rtl8xxxu_write32_mask(priv, C.REG_DPDT_CTRL, 0x8000000, 0x0)

    if priv.rf_paths > 1:
        rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0x000000)
        rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_AC, 0x10000)
        rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0x808000)

    for _ in range(retry):
        path_a_ok = rtl8192fu_iqk_path_a(priv)
        if path_a_ok == 0x01:
            val32 = rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_A)
            result[t][0] = (val32 >> 16) & 0x3FF
            val32 = rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_A)
            result[t][1] = (val32 >> 16) & 0x3FF
            break
        result[t][0] = 0x100
        result[t][1] = 0x0

    for _ in range(retry):
        path_a_ok = rtl8192fu_rx_iqk_path_a(priv)
        if path_a_ok == 0x03:
            val32 = rtl8xxxu_read32(priv, C.REG_RX_POWER_BEFORE_IQK_A_2)
            result[t][2] = (val32 >> 16) & 0x3FF
            val32 = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_A_2)
            result[t][3] = (val32 >> 16) & 0x3FF
            break
        result[t][2] = 0x100
        result[t][3] = 0x0

    if priv.rf_paths > 1:
        for _ in range(retry):
            path_b_ok = rtl8192fu_iqk_path_b(priv)
            if path_b_ok == 0x01:
                val32 = rtl8xxxu_read32(priv, C.REG_TX_POWER_BEFORE_IQK_B)
                result[t][4] = (val32 >> 16) & 0x3FF
                val32 = rtl8xxxu_read32(priv, C.REG_TX_POWER_AFTER_IQK_B)
                result[t][5] = (val32 >> 16) & 0x3FF
                break
            result[t][4] = 0x100
            result[t][5] = 0x0

        for _ in range(retry):
            path_b_ok = rtl8192fu_rx_iqk_path_b(priv)
            if path_b_ok == 0x03:
                val32 = rtl8xxxu_read32(priv, C.REG_RX_POWER_BEFORE_IQK_B_2)
                result[t][6] = (val32 >> 16) & 0x3FF
                val32 = rtl8xxxu_read32(priv, C.REG_RX_POWER_AFTER_IQK_B_2)
                result[t][7] = (val32 >> 16) & 0x3FF
                break
            result[t][6] = 0x100
            result[t][7] = 0x0

    rtl8xxxu_write32_mask(priv, C.REG_FPGA0_IQK, 0xFFFFFF00, 0)

    rtl8xxxu_write32(priv, C.REG_FPGA0_ANALOG4, 0xCC0000C0)

    rtl8xxxu_write32(priv, C.REG_ANAPWR1, 0x44BBBB44)
    rtl8xxxu_write32(priv, C.REG_RX_WAIT_CCA, 0x80408040)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TRX_PATH_ENABLE, 0x6F005433)
    rtl8xxxu_write32(priv, C.REG_OFDM0_TR_MUX_PAR, 0x000004E4)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XCD_RF_SW_CTRL, 0x04003400)
    rtl8xxxu_write32(priv, C.REG_FPGA0_XA_HSSI_PARM1, 0x01000100)

    rtl8xxxu_restore_regs(priv, ADDA_REGS, priv.adda_backup, len(ADDA_REGS))
    rtl8xxxu_restore_mac_regs(priv, IQK_MAC_REGS, priv.mac_backup)
    rtl8xxxu_restore_regs(priv, IQK_BB_REGS, priv.bb_backup, C.RTL8XXXU_BB_REGS)

    rtl8xxxu_write32_clear(priv, C.REG_FPGA0_XCD_RF_PARM, C.BIT(31))

    rtl8xxxu_write32_mask(priv, C.REG_OFDM0_XA_AGC_CORE1, 0xFF, 0x50)
    rtl8xxxu_write32_mask(priv, C.REG_OFDM0_XA_AGC_CORE1, 0xFF, rx_initial_gain_a & 0xFF)
    if priv.rf_paths > 1:
        rtl8xxxu_write32_mask(priv, C.REG_OFDM0_XB_AGC_CORE1, 0xFF, 0x50)
        rtl8xxxu_write32_mask(priv, C.REG_OFDM0_XB_AGC_CORE1, 0xFF, rx_initial_gain_b & 0xFF)


def rtl8192fu_phy_iq_calibrate(priv: Rtl8xxxuPriv) -> None:
    rfe = priv.rfe_type
    result = [[0] * 8 for _ in range(4)]
    candidate = -1
    path_a_ok = False
    path_b_ok = False
    reg_tmp = 0

    rfe_path_select = rtl8xxxu_read32(priv, C.REG_RFE_PATH_SELECT)

    path_a_0xdf = rtl8xxxu_read_rfreg(priv, RF_A, C.RF6052_REG_GAIN_CCA)
    path_a_0x35 = rtl8xxxu_read_rfreg(priv, RF_A, C.RF6052_REG_GAIN_P1)
    path_b_0xdf = rtl8xxxu_read_rfreg(priv, RF_B, C.RF6052_REG_GAIN_CCA)
    path_b_0x35 = rtl8xxxu_read_rfreg(priv, RF_B, C.RF6052_REG_GAIN_P1)

    for i in range(3):
        rtl8192fu_phy_iqcalibrate(priv, result, i)

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
                reg_tmp = sum(result[3])
                candidate = 3 if reg_tmp else -1

    reg_e94 = reg_ea4 = reg_eb4 = reg_ec4 = 0
    if candidate >= 0:
        reg_e94 = result[candidate][0]
        reg_ea4 = result[candidate][2]
        reg_eb4 = result[candidate][4]
        reg_ec4 = result[candidate][6]
        path_a_ok = True
        path_b_ok = True

    rtl8xxxu_write32_mask(priv, C.REG_TX_IQK_TONE_A, 0x3FF00000, 0x100)
    rtl8xxxu_write32_mask(priv, C.REG_NP_ANTA, 0x3FF, 0)
    rtl8xxxu_write32_mask(priv, C.REG_TX_IQK_TONE_B, 0x3FF00000, 0x100)
    rtl8xxxu_write32_mask(priv, C.REG_TAP_UPD_97F, 0x3FF, 0)

    if candidate >= 0:
        if reg_e94:
            rtl8xxxu_fill_iqk_matrix_a(priv, path_a_ok, result, candidate, reg_ea4 == 0)
        if reg_eb4:
            rtl8xxxu_fill_iqk_matrix_b(priv, path_b_ok, result, candidate, reg_ec4 == 0)

    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_GAIN_CCA, path_a_0xdf)
    rtl8xxxu_write_rfreg(priv, RF_A, C.RF6052_REG_GAIN_P1, path_a_0x35)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_GAIN_CCA, path_b_0xdf)
    rtl8xxxu_write_rfreg(priv, RF_B, C.RF6052_REG_GAIN_P1, path_b_0x35)

    if _is_epa(rfe):
        # TODO: verify, untested here, needs an ePA board (rfe_type 7/8/9/12), 8192f.c:1542
        rtl8xxxu_write32_set(priv, C.REG_SW_GPIO_SHARE_CTRL_1, 0x70000)
        rtl8xxxu_write32_clear(priv, C.REG_LEDCFG0, 0x6C00000)
        rtl8xxxu_write32_set(priv, C.REG_PAD_CTRL1, C.BIT(29) | C.BIT(28))
        rtl8xxxu_write32_clear(priv, C.REG_SW_GPIO_SHARE_CTRL_0, 0x600000 | C.BIT(4))

        val32 = rtl8xxxu_read32(priv, C.REG_RFE_BUFFER)
        val32 &= ~C.BIT(11)
        val32 |= 0x1F
        rtl8xxxu_write32(priv, C.REG_RFE_BUFFER, val32)

        if rfe == 7:
            rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANTA_SRC, 0xFFFFF, 0x23200)
            rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANT_SRC2, 0xFFFFF, 0x23200)
            rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANT_SRC1, 0xF000, 0x3)
            rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANT_SRC3, 0xF000, 0x3)
        else:
            rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANTA_SRC, 0xFFFFF, 0x22200)
            rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANT_SRC2, 0xFFFFF, 0x22200)
            rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANT_SRC1, 0xF000, 0x2)
            rtl8xxxu_write32_mask(priv, C.REG_RFE_CTRL_ANT_SRC3, 0xF000, 0x2)

        rtl8xxxu_write32_clear(priv, C.REG_RFE_OPT62, C.BIT(2))

        if rfe == 7:
            rtl8xxxu_write32(priv, C.REG_RFE_OPT, 0x03000003)

        rtl8xxxu_write32(priv, C.REG_RFE_PATH_SELECT, rfe_path_select)
