"""RTL8188E IQ calibration (IQK), 1T1R (``is2T`` = false) [SRC] halrf_8188e_ce.c:574-1484.

Reached from the power-track callback (|delta_IQK| >= 8) via ``do_iqk_8188e`` ->
``halrf_iqk_trigger`` [SRC] halrf.c:1309 -> ``phy_iq_calibrate_8188e``. The carried state
(backups, results matrix, in-progress flag) lives on ``powertrack.PowerTrackState``, the
port of ``dm_rf_calibration_struct``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from . import bb, rf
from .constants import BIT

if TYPE_CHECKING:
    from .powertrack import PowerTrackState

# [SRC] halrf_powertracking_ce.h:45-56, halrf_8188e_ce.h:20, halrf_8188e_ce.c:574.
IQK_MAC_REG_NUM = 4
IQK_ADDA_REG_NUM = 16
IQK_BB_REG_NUM = 9
iqk_matrix_reg_num = 8
IQK_MATRIX_SETTINGS_NUM = 14 + 24 + 21
IQK_DELAY_TIME_88E_MS = 10
MAX_TOLERANCE = 5

MASKDWORD = 0xFFFFFFFF
MASKH3BYTES = 0xFFFFFF00
RFREGOFFSETMASK = 0xFFFFF
RF_PATH_A = 0

# RF regs [SRC] include/Hal8188EPhyReg.h:430-444.
RF_RCK_OS = 0x30
RF_TXPA_G1 = 0x31
RF_TXPA_G2 = 0x32
RF_WE_LUT = 0xEF

# MAC regs [SRC] include/hal_com_reg.h.
REG_GPIO_MUXCFG = 0x0040
REG_TXPAUSE = 0x0522
REG_BCN_CTRL = 0x0550
REG_BCN_CTRL_1 = 0x0551

# BB regs [SRC] include/Hal8188EPhyReg.h.
REG_FPGA0_XA_HSSI_PARAMETER1 = 0x0820
REG_FPGA0_XB_HSSI_PARAMETER1 = 0x0828
REG_FPGA0_XA_LSSI_PARAMETER = 0x0840
REG_FPGA0_XCD_SWITCH_CONTROL = 0x085C
REG_FPGA0_XA_RF_INTERFACE_OE = 0x0860
REG_FPGA0_XB_RF_INTERFACE_OE = 0x0864
REG_FPGA0_XAB_RF_INTERFACE_SW = 0x0870
REG_FPGA0_XCD_RF_INTERFACE_SW = 0x0874
REG_CCK_0_AFE_SETTING = 0x0A04
REG_CONFIG_ANT_A = 0x0B68
REG_CONFIG_ANT_B = 0x0B6C
REG_OFDM_0_TRX_PATH_ENABLE = 0x0C04
REG_OFDM_0_TR_MUX_PAR = 0x0C08
REG_OFDM_0_XA_RX_IQ_IMBALANCE = 0x0C14
REG_OFDM_0_XB_RX_IQ_IMBALANCE = 0x0C1C
REG_OFDM_0_ECCA_THRESHOLD = 0x0C4C
REG_OFDM_0_AGC_RSSI_TABLE = 0x0C78
REG_OFDM_0_XA_TX_IQ_IMBALANCE = 0x0C80
REG_OFDM_0_XB_TX_IQ_IMBALANCE = 0x0C88
REG_OFDM_0_XC_TX_AFE = 0x0C94
REG_OFDM_0_XD_TX_AFE = 0x0C9C
REG_OFDM_0_RX_IQ_EXT_ANTA = 0x0CA0
REG_FPGA0_IQK = 0x0E28
REG_TX_IQK_TONE_A = 0x0E30
REG_RX_IQK_TONE_A = 0x0E34
REG_TX_IQK_PI_A = 0x0E38
REG_RX_IQK_PI_A = 0x0E3C
REG_TX_IQK = 0x0E40
REG_RX_IQK = 0x0E44
REG_IQK_AGC_PTS = 0x0E48
REG_IQK_AGC_RSP = 0x0E4C
REG_BLUE_TOOTH = 0x0E6C
REG_RX_WAIT_CCA = 0x0E70
REG_TX_CCK_RFON = 0x0E74
REG_TX_CCK_BBON = 0x0E78
REG_TX_OFDM_RFON = 0x0E7C
REG_TX_OFDM_BBON = 0x0E80
REG_TX_TO_RX = 0x0E84
REG_TX_TO_TX = 0x0E88
REG_RX_CCK = 0x0E8C
REG_TX_POWER_BEFORE_IQK_A = 0x0E94
REG_TX_POWER_AFTER_IQK_A = 0x0E9C
REG_RX_POWER_BEFORE_IQK_A_2 = 0x0EA4
REG_RX_POWER_AFTER_IQK_A_2 = 0x0EAC
REG_RX_OFDM = 0x0ED0
REG_RX_WAIT_RIFS = 0x0ED4
REG_RX_TO_RX = 0x0ED8
REG_STANDBY = 0x0EDC
REG_SLEEP = 0x0EE0
REG_PMPD_ANAEN = 0x0EEC

# _phy_iq_calibrate_8188e locals [SRC] halrf_8188e_ce.c:1135-1157.
ADDA_REG = (
    REG_FPGA0_XCD_SWITCH_CONTROL, REG_BLUE_TOOTH,
    REG_RX_WAIT_CCA, REG_TX_CCK_RFON,
    REG_TX_CCK_BBON, REG_TX_OFDM_RFON,
    REG_TX_OFDM_BBON, REG_TX_TO_RX,
    REG_TX_TO_TX, REG_RX_CCK,
    REG_RX_OFDM, REG_RX_WAIT_RIFS,
    REG_RX_TO_RX, REG_STANDBY,
    REG_SLEEP, REG_PMPD_ANAEN,
)
IQK_MAC_REG = (REG_TXPAUSE, REG_BCN_CTRL, REG_BCN_CTRL_1, REG_GPIO_MUXCFG)
IQK_BB_REG_92C = (
    REG_OFDM_0_TRX_PATH_ENABLE, REG_OFDM_0_TR_MUX_PAR,
    REG_FPGA0_XCD_RF_INTERFACE_SW, REG_CONFIG_ANT_A, REG_CONFIG_ANT_B,
    REG_FPGA0_XAB_RF_INTERFACE_SW, REG_FPGA0_XA_RF_INTERFACE_OE,
    REG_FPGA0_XB_RF_INTERFACE_OE, REG_CCK_0_AFE_SETTING,
)


def _u32(v: int) -> int:
    return v & 0xFFFFFFFF


@dataclass
class IqkMatrixRegsSetting:
    """``struct iqk_matrix_regs_setting`` [SRC] halrf_powertracking_ce.h:84."""
    is_iqk_done: bool = False
    value: list[list[int]] = field(
        default_factory=lambda: [[0] * iqk_matrix_reg_num for _ in range(3)])


def odm_get_right_chnl_place_for_iqk(chnl: int) -> int:
    """[SRC] halphyrf_ce.c:829."""
    channel_all = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 36, 38, 40, 42, 44, 46, 48,
                   50, 52, 54, 56, 58, 60, 62, 64, 100, 102, 104, 106, 108, 110, 112, 114, 116,
                   118, 120, 122, 124, 126, 128, 130, 132, 134, 136, 138, 140, 149, 151, 153,
                   155, 157, 159, 161, 163, 165)
    if chnl > 14:
        for place in range(14, len(channel_all)):
            if channel_all[place] == chnl:
                return place - 13
    return 0


def do_iqk_8188e(t, cali_info: PowerTrackState, delta_thermal_index: int, thermal_value: int,
                 threshold: int) -> None:
    """[SRC] halrf_8188e_ce.c:143. ``odm_reset_iqk_result`` is an empty stub (halphyrf_ce.c:821)."""
    cali_info.thermal_value_iqk = thermal_value
    halrf_iqk_trigger(t, cali_info, False)


def halrf_iqk_trigger(t, cali_info: PowerTrackState, is_recovery: bool) -> None:
    """[SRC] halrf.c:1309 (8188E case). The mp_mode / rf_supportability / rfk_forbidden gates
    are not tracked: no MP mode, HAL_RF_IQK set, rfk_forbidden never set."""
    if cali_info.is_iqk_in_progress:
        return
    cali_info.is_iqk_in_progress = True
    try:
        phy_iq_calibrate_8188e(t, cali_info, is_recovery)
    finally:
        cali_info.is_iqk_in_progress = False


def phy_path_a_iqk_8188e(t) -> int:
    """[SRC] halrf_8188e_ce.c:578. bit0 = Tx OK."""
    result = 0x00
    bb.set_bb_reg(t, REG_TX_IQK_TONE_A, MASKDWORD, 0x10008c1c)
    bb.set_bb_reg(t, REG_RX_IQK_TONE_A, MASKDWORD, 0x30008c1c)
    bb.set_bb_reg(t, REG_TX_IQK_PI_A, MASKDWORD, 0x8214032a)
    bb.set_bb_reg(t, REG_RX_IQK_PI_A, MASKDWORD, 0x28160000)
    bb.set_bb_reg(t, REG_IQK_AGC_RSP, MASKDWORD, 0x00462911)
    bb.set_bb_reg(t, REG_IQK_AGC_PTS, MASKDWORD, 0xf9000000)
    bb.set_bb_reg(t, REG_IQK_AGC_PTS, MASKDWORD, 0xf8000000)
    time.sleep(IQK_DELAY_TIME_88E_MS / 1000)

    reg_eac = bb.query_bb_reg(t, REG_RX_POWER_AFTER_IQK_A_2, MASKDWORD)
    reg_e94 = bb.query_bb_reg(t, REG_TX_POWER_BEFORE_IQK_A, MASKDWORD)
    reg_e9c = bb.query_bb_reg(t, REG_TX_POWER_AFTER_IQK_A, MASKDWORD)
    bb.query_bb_reg(t, REG_RX_POWER_BEFORE_IQK_A_2, MASKDWORD)    # reg_ea4, unused

    if (not (reg_eac & BIT(28))
            and ((reg_e94 & 0x03FF0000) >> 16) != 0x142
            and ((reg_e9c & 0x03FF0000) >> 16) != 0x42):
        result |= 0x01
    return result


def phy_path_a_rx_iqk(t) -> int:
    """[SRC] halrf_8188e_ce.c:643. bit0 = Tx OK, bit1 = Rx OK."""
    result = 0x00
    # Get TXIMR setting: modify RXIQK mode table.
    bb.set_bb_reg(t, REG_FPGA0_IQK, MASKH3BYTES, 0x000000)
    rf.set_rf_reg(t, RF_PATH_A, RF_WE_LUT, RFREGOFFSETMASK, 0x800a0)
    rf.set_rf_reg(t, RF_PATH_A, RF_RCK_OS, RFREGOFFSETMASK, 0x30000)
    rf.set_rf_reg(t, RF_PATH_A, RF_TXPA_G1, RFREGOFFSETMASK, 0x0000f)
    rf.set_rf_reg(t, RF_PATH_A, RF_TXPA_G2, RFREGOFFSETMASK, 0xf117B)
    bb.set_bb_reg(t, REG_FPGA0_IQK, MASKH3BYTES, 0x808000)
    bb.set_bb_reg(t, REG_TX_IQK, MASKDWORD, 0x01007c00)
    bb.set_bb_reg(t, REG_RX_IQK, MASKDWORD, 0x81004800)
    bb.set_bb_reg(t, REG_TX_IQK_TONE_A, MASKDWORD, 0x10008c1c)
    bb.set_bb_reg(t, REG_RX_IQK_TONE_A, MASKDWORD, 0x30008c1c)
    bb.set_bb_reg(t, REG_TX_IQK_PI_A, MASKDWORD, 0x82160804)
    bb.set_bb_reg(t, REG_RX_IQK_PI_A, MASKDWORD, 0x28160000)
    bb.set_bb_reg(t, REG_IQK_AGC_RSP, MASKDWORD, 0x0046a911)
    bb.set_bb_reg(t, REG_IQK_AGC_PTS, MASKDWORD, 0xf9000000)
    bb.set_bb_reg(t, REG_IQK_AGC_PTS, MASKDWORD, 0xf8000000)
    time.sleep(IQK_DELAY_TIME_88E_MS / 1000)

    reg_eac = bb.query_bb_reg(t, REG_RX_POWER_AFTER_IQK_A_2, MASKDWORD)
    reg_e94 = bb.query_bb_reg(t, REG_TX_POWER_BEFORE_IQK_A, MASKDWORD)
    reg_e9c = bb.query_bb_reg(t, REG_TX_POWER_AFTER_IQK_A, MASKDWORD)

    if (not (reg_eac & BIT(28))
            and ((reg_e94 & 0x03FF0000) >> 16) != 0x142
            and ((reg_e9c & 0x03FF0000) >> 16) != 0x42):
        result |= 0x01
    else:
        return result

    u4tmp = 0x80007C00 | (reg_e94 & 0x3FF0000) | ((reg_e9c & 0x3FF0000) >> 16)
    bb.set_bb_reg(t, REG_TX_IQK, MASKDWORD, u4tmp)

    # RX IQK: modify RXIQK mode table 2.
    bb.set_bb_reg(t, REG_FPGA0_IQK, MASKH3BYTES, 0x000000)
    rf.set_rf_reg(t, RF_PATH_A, RF_WE_LUT, RFREGOFFSETMASK, 0x800a0)
    rf.set_rf_reg(t, RF_PATH_A, RF_RCK_OS, RFREGOFFSETMASK, 0x30000)
    rf.set_rf_reg(t, RF_PATH_A, RF_TXPA_G1, RFREGOFFSETMASK, 0x0000f)
    rf.set_rf_reg(t, RF_PATH_A, RF_TXPA_G2, RFREGOFFSETMASK, 0xf7ffa)
    bb.set_bb_reg(t, REG_FPGA0_IQK, MASKH3BYTES, 0x808000)
    bb.set_bb_reg(t, REG_RX_IQK, MASKDWORD, 0x01004800)
    bb.set_bb_reg(t, REG_TX_IQK_TONE_A, MASKDWORD, 0x30008c1c)
    bb.set_bb_reg(t, REG_RX_IQK_TONE_A, MASKDWORD, 0x10008c1c)
    bb.set_bb_reg(t, REG_TX_IQK_PI_A, MASKDWORD, 0x82160c05)
    bb.set_bb_reg(t, REG_RX_IQK_PI_A, MASKDWORD, 0x28160c05)
    bb.set_bb_reg(t, REG_IQK_AGC_RSP, MASKDWORD, 0x0046a911)
    bb.set_bb_reg(t, REG_IQK_AGC_PTS, MASKDWORD, 0xf9000000)
    bb.set_bb_reg(t, REG_IQK_AGC_PTS, MASKDWORD, 0xf8000000)
    time.sleep(IQK_DELAY_TIME_88E_MS / 1000)

    reg_eac = bb.query_bb_reg(t, REG_RX_POWER_AFTER_IQK_A_2, MASKDWORD)
    bb.query_bb_reg(t, REG_TX_POWER_BEFORE_IQK_A, MASKDWORD)       # reg_e94, unused
    bb.query_bb_reg(t, REG_TX_POWER_AFTER_IQK_A, MASKDWORD)        # reg_e9c, unused
    reg_ea4 = bb.query_bb_reg(t, REG_RX_POWER_BEFORE_IQK_A_2, MASKDWORD)

    if (not (reg_eac & BIT(27))
            and ((reg_ea4 & 0x03FF0000) >> 16) != 0x132
            and ((reg_eac & 0x03FF0000) >> 16) != 0x36):
        result |= 0x02
    return result


def _phy_path_a_fill_iqk_matrix(t, is_iqk_ok: bool, result: list[list[int]],
                                final_candidate: int, is_tx_only: bool) -> None:
    """[SRC] halrf_8188e_ce.c:844. X/TX0_A are C ``u32``: the products wrap at 32 bits."""
    if final_candidate == 0xFF:
        return
    if not is_iqk_ok:
        return
    oldval_0 = (bb.query_bb_reg(t, REG_OFDM_0_XA_TX_IQ_IMBALANCE, MASKDWORD) >> 22) & 0x3FF

    X = _u32(result[final_candidate][0])
    if (X & 0x00000200) != 0:
        X = X | 0xFFFFFC00
    TX0_A = _u32(X * oldval_0) >> 8
    bb.set_bb_reg(t, REG_OFDM_0_XA_TX_IQ_IMBALANCE, 0x3FF, TX0_A)
    bb.set_bb_reg(t, REG_OFDM_0_ECCA_THRESHOLD, BIT(31), (_u32(X * oldval_0) >> 7) & 0x1)

    # Y is s32, but Y * oldval_0 promotes to u32.
    Y = _u32(result[final_candidate][1])
    if (Y & 0x00000200) != 0:
        Y = Y | 0xFFFFFC00
    TX0_C = _u32(Y * oldval_0) >> 8
    bb.set_bb_reg(t, REG_OFDM_0_XC_TX_AFE, 0xF0000000, (TX0_C & 0x3C0) >> 6)
    bb.set_bb_reg(t, REG_OFDM_0_XA_TX_IQ_IMBALANCE, 0x003F0000, TX0_C & 0x3F)
    bb.set_bb_reg(t, REG_OFDM_0_ECCA_THRESHOLD, BIT(29), (_u32(Y * oldval_0) >> 7) & 0x1)

    if is_tx_only:
        return

    reg = _u32(result[final_candidate][2])
    bb.set_bb_reg(t, REG_OFDM_0_XA_RX_IQ_IMBALANCE, 0x3FF, reg)
    reg = result[final_candidate][3] & 0x3F
    bb.set_bb_reg(t, REG_OFDM_0_XA_RX_IQ_IMBALANCE, 0xFC00, reg)
    reg = (result[final_candidate][3] >> 6) & 0xF
    bb.set_bb_reg(t, REG_OFDM_0_RX_IQ_EXT_ANTA, 0xF0000000, reg)


def _phy_save_adda_registers(t, adda_reg, adda_backup: list[int], register_num: int) -> None:
    for i in range(register_num):
        adda_backup[i] = bb.query_bb_reg(t, adda_reg[i], MASKDWORD)


def _phy_save_mac_registers(t, mac_reg, mac_backup: list[int]) -> None:
    for i in range(IQK_MAC_REG_NUM - 1):
        mac_backup[i] = t.read8(mac_reg[i])
    i = IQK_MAC_REG_NUM - 1
    mac_backup[i] = t.read32(mac_reg[i])


def _phy_reload_adda_registers(t, adda_reg, adda_backup: list[int], regiester_num: int) -> None:
    for i in range(regiester_num):
        bb.set_bb_reg(t, adda_reg[i], MASKDWORD, adda_backup[i])


def _phy_reload_mac_registers(t, mac_reg, mac_backup: list[int]) -> None:
    for i in range(IQK_MAC_REG_NUM - 1):
        t.write8(mac_reg[i], mac_backup[i] & 0xFF)
    i = IQK_MAC_REG_NUM - 1
    t.write32(mac_reg[i], mac_backup[i])


def _phy_path_adda_on(t, adda_reg) -> None:
    """[SRC] halrf_8188e_ce.c:996, is2T = false (``is_path_a_on`` is then unused)."""
    path_on = 0x0bdb25a0
    bb.set_bb_reg(t, adda_reg[0], MASKDWORD, 0x0b1b25a0)
    for i in range(1, IQK_ADDA_REG_NUM):
        bb.set_bb_reg(t, adda_reg[i], MASKDWORD, path_on)


def _phy_mac_setting_calibration(t, mac_reg, mac_backup: list[int]) -> None:
    t.write8(mac_reg[0], 0x3F)
    for i in range(1, IQK_MAC_REG_NUM - 1):
        t.write8(mac_reg[i], mac_backup[i] & ~BIT(3) & 0xFF)
    i = IQK_MAC_REG_NUM - 1
    t.write8(mac_reg[i], mac_backup[i] & ~BIT(5) & 0xFF)


def _phy_pi_mode_switch(t, pi_mode: bool) -> None:
    mode = 0x01000100 if pi_mode else 0x01000000
    bb.set_bb_reg(t, REG_FPGA0_XA_HSSI_PARAMETER1, MASKDWORD, mode)
    bb.set_bb_reg(t, REG_FPGA0_XB_HSSI_PARAMETER1, MASKDWORD, mode)


def phy_simularity_compare_8188e(result: list[list[int]], c1: int, c2: int) -> bool:
    """[SRC] halrf_8188e_ce.c:1063, is2T = false (bound 4). Writes ``result[3]``."""
    final_candidate = [0xFF, 0xFF]
    is_result = True
    bound = 4
    simularity_bit_map = 0
    for i in range(bound):
        a, b = result[c1][i], result[c2][i]
        diff = _u32(a - b) if a > b else _u32(b - a)
        if diff > MAX_TOLERANCE:
            if (i == 2 or i == 6) and not simularity_bit_map:
                if result[c1][i] + result[c1][i + 1] == 0:
                    final_candidate[i // 4] = c2
                elif result[c2][i] + result[c2][i + 1] == 0:
                    final_candidate[i // 4] = c1
                else:
                    simularity_bit_map |= 1 << i
            else:
                simularity_bit_map |= 1 << i

    if simularity_bit_map == 0:
        for i in range(bound // 4):
            if final_candidate[i] != 0xFF:
                for j in range(i * 4, (i + 1) * 4 - 2):
                    result[3][j] = result[final_candidate[i]][j]
                is_result = False
        return is_result
    if not (simularity_bit_map & 0x0F):        # path A OK
        for i in range(4):
            result[3][i] = result[c1][i]
        return False
    return False


def _phy_iq_calibrate_8188e(t, cali_info: PowerTrackState, result: list[list[int]],
                            t_idx: int) -> None:
    """[SRC] halrf_8188e_ce.c:1127, is2T = false. ``t_idx`` is the C ``t`` (run number);
    only runs after the first restore the saved ADDA/MAC/BB registers."""
    retry_count = 2

    if t_idx == 0:
        _phy_save_adda_registers(t, ADDA_REG, cali_info.ADDA_backup, IQK_ADDA_REG_NUM)
        _phy_save_mac_registers(t, IQK_MAC_REG, cali_info.IQK_MAC_backup)
        _phy_save_adda_registers(t, IQK_BB_REG_92C, cali_info.IQK_BB_backup, IQK_BB_REG_NUM)
    _phy_path_adda_on(t, ADDA_REG)

    if t_idx == 0:
        cali_info.is_rf_pi_enable = bb.query_bb_reg(
            t, REG_FPGA0_XA_HSSI_PARAMETER1, BIT(8)) & 0xFF
    if not cali_info.is_rf_pi_enable:
        _phy_pi_mode_switch(t, True)

    _phy_mac_setting_calibration(t, IQK_MAC_REG, cali_info.IQK_MAC_backup)
    bb.set_bb_reg(t, REG_CCK_0_AFE_SETTING, 0x0f000000, 0xf)
    bb.set_bb_reg(t, REG_OFDM_0_TRX_PATH_ENABLE, MASKDWORD, 0x03a05600)
    bb.set_bb_reg(t, REG_OFDM_0_TR_MUX_PAR, MASKDWORD, 0x000800e4)
    bb.set_bb_reg(t, REG_FPGA0_XCD_RF_INTERFACE_SW, MASKDWORD, 0x22204000)

    bb.set_bb_reg(t, REG_FPGA0_XAB_RF_INTERFACE_SW, BIT(10), 0x01)
    bb.set_bb_reg(t, REG_FPGA0_XAB_RF_INTERFACE_SW, BIT(26), 0x01)
    bb.set_bb_reg(t, REG_FPGA0_XA_RF_INTERFACE_OE, BIT(10), 0x00)
    bb.set_bb_reg(t, REG_FPGA0_XB_RF_INTERFACE_OE, BIT(10), 0x00)

    bb.set_bb_reg(t, REG_CONFIG_ANT_A, MASKDWORD, 0x0f600000)

    bb.set_bb_reg(t, REG_FPGA0_IQK, MASKH3BYTES, 0x808000)
    bb.set_bb_reg(t, REG_TX_IQK, MASKDWORD, 0x01007c00)
    bb.set_bb_reg(t, REG_RX_IQK, MASKDWORD, 0x81004800)

    for _ in range(retry_count):
        path_aok = phy_path_a_iqk_8188e(t)
        if path_aok == 0x01:
            result[t_idx][0] = (bb.query_bb_reg(t, REG_TX_POWER_BEFORE_IQK_A, MASKDWORD)
                                & 0x3FF0000) >> 16
            result[t_idx][1] = (bb.query_bb_reg(t, REG_TX_POWER_AFTER_IQK_A, MASKDWORD)
                                & 0x3FF0000) >> 16
            break

    for _ in range(retry_count):
        path_aok = phy_path_a_rx_iqk(t)
        if path_aok == 0x03:
            result[t_idx][2] = (bb.query_bb_reg(t, REG_RX_POWER_BEFORE_IQK_A_2, MASKDWORD)
                                & 0x3FF0000) >> 16
            result[t_idx][3] = (bb.query_bb_reg(t, REG_RX_POWER_AFTER_IQK_A_2, MASKDWORD)
                                & 0x3FF0000) >> 16
            break

    # Back to BB mode, load original value.
    bb.set_bb_reg(t, REG_FPGA0_IQK, MASKH3BYTES, 0)

    if t_idx != 0:
        if not cali_info.is_rf_pi_enable:
            _phy_pi_mode_switch(t, False)
        _phy_reload_adda_registers(t, ADDA_REG, cali_info.ADDA_backup, IQK_ADDA_REG_NUM)
        _phy_reload_mac_registers(t, IQK_MAC_REG, cali_info.IQK_MAC_backup)
        _phy_reload_adda_registers(t, IQK_BB_REG_92C, cali_info.IQK_BB_backup, IQK_BB_REG_NUM)
        bb.set_bb_reg(t, REG_FPGA0_XA_LSSI_PARAMETER, MASKDWORD, 0x00032ed3)
        bb.set_bb_reg(t, REG_TX_IQK_TONE_A, MASKDWORD, 0x01008c00)
        bb.set_bb_reg(t, REG_RX_IQK_TONE_A, MASKDWORD, 0x01008c00)


def phy_iq_calibrate_8188e(t, cali_info: PowerTrackState, is_recovery: bool) -> None:
    """[SRC] halrf_8188e_ce.c:1360. Up to three runs; the first two (or any two of three)
    that agree within MAX_TOLERANCE give the result, stored per channel in
    ``iqk_matrix_reg_setting`` for power tracking's ``set_iqk_matrix_8188e``."""
    IQK_BB_REG_92C = (                          # not _phy_iq_calibrate_8188e's list
        REG_OFDM_0_XA_RX_IQ_IMBALANCE, REG_OFDM_0_XB_RX_IQ_IMBALANCE,
        REG_OFDM_0_ECCA_THRESHOLD, REG_OFDM_0_AGC_RSSI_TABLE,
        REG_OFDM_0_XA_TX_IQ_IMBALANCE, REG_OFDM_0_XB_TX_IQ_IMBALANCE,
        REG_OFDM_0_XC_TX_AFE, REG_OFDM_0_XD_TX_AFE,
        REG_OFDM_0_RX_IQ_EXT_ANTA,
    )
    if is_recovery:
        _phy_reload_adda_registers(t, IQK_BB_REG_92C, cali_info.IQK_BB_backup_recover, 9)
        return

    result = [[0] * 8 for _ in range(4)]       # s32 result[4][8]; [3] is the final result
    final_candidate = 0xFF

    for i in range(3):
        _phy_iq_calibrate_8188e(t, cali_info, result, i)
        if i == 1:
            if phy_simularity_compare_8188e(result, 0, 1):
                final_candidate = 0
                break
        if i == 2:
            if phy_simularity_compare_8188e(result, 0, 2):
                final_candidate = 0
                break
            if phy_simularity_compare_8188e(result, 1, 2):
                final_candidate = 1
            else:
                # The C sums with the outer loop counter i, ending the loop (i == 2 is last anyway).
                reg_tmp = sum(result[3])
                final_candidate = 3 if reg_tmp != 0 else 0xFF

    # The C debug-print loop leaves rege94/regea4 = result[3][...].
    rege94 = result[3][0]
    regea4 = result[3][2]

    if final_candidate != 0xFF:
        cali_info.rege94 = rege94 = result[final_candidate][0]
        cali_info.rege9c = result[final_candidate][1]
        regea4 = result[final_candidate][2]
        cali_info.regeb4 = result[final_candidate][4]
        cali_info.regebc = result[final_candidate][5]
        is_patha_ok = True
    else:
        cali_info.rege94 = cali_info.regeb4 = 0x100
        cali_info.rege9c = cali_info.regebc = 0x0
        is_patha_ok = False

    if rege94 != 0:
        _phy_path_a_fill_iqk_matrix(t, is_patha_ok, result, final_candidate, regea4 == 0)

    indexforchannel = odm_get_right_chnl_place_for_iqk(cali_info.channel)

    if final_candidate < 4:
        setting = cali_info.iqk_matrix_reg_setting[indexforchannel]
        for i in range(iqk_matrix_reg_num):
            setting.value[0][i] = result[final_candidate][i]
        setting.is_iqk_done = True

    _phy_save_adda_registers(t, IQK_BB_REG_92C, cali_info.IQK_BB_backup_recover,
                             IQK_BB_REG_NUM)
