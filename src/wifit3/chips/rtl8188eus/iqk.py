"""RTL8188EUS IQ calibration (path A).

Cleanroom port of the 8188e IQK + the shared gen1 ``rtl8xxxu`` IQK helpers:

* ``rtl8188eu_phy_iq_calibrate``  — ``8188e.c:906`` (3-iteration wrapper + similarity)
* ``rtl8188eu_phy_iqcalibrate``   — ``8188e.c:750`` (one iteration: backup → cal → restore)
* ``rtl8188eu_iqk_path_a``        — ``8188e.c:610`` (TX IQK path A)
* ``rtl8188eu_rx_iqk_path_a``     — ``8188e.c:644`` (RX IQK path A)
* ``rtl8xxxu_save_regs`` / ``save_mac_regs`` / ``restore_regs`` / ``restore_mac_regs``
                                   — ``core.c:3037 / 3015 / 3046 / 3026``
* ``rtl8xxxu_path_adda_on``       — ``core.c:3056``
* ``rtl8xxxu_mac_calibration``    — ``core.c:3076``

IQK is a closed-loop calibration: write a trigger, the chip measures the path-A TX/RX
imbalance, the driver reads the result registers (``0xe94``/``0xe9c``/``0xea4``/``0xeac``)
and either accepts or retries, then a final pass fills the OFDM correction matrix. Because
every measurement-read and correction-write is on the wire, this replays against the
cold-boot capture: ``verify_pcap`` serves the recorded reads, a faithful algorithm
reproduces the recorded writes.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .constants import (
    ADDA_1T_INIT,
    ADDA_1T_PATH_ON,
    FPGA0_HSSI_PARM1_PI,
    FPGA0_RF_BD_CTRL_SHIFT,
    FPGA0_RF_PAPE,
    REG_BEACON_CTRL,
    REG_BEACON_CTRL_1,
    REG_BLUETOOTH,
    REG_CCK0_AFE_SETTING,
    REG_CONFIG_ANT_A,
    REG_CONFIG_ANT_B,
    REG_FPGA0_IQK,
    REG_FPGA0_XA_HSSI_PARM1,
    REG_FPGA0_XA_LSSI_PARM,
    REG_FPGA0_XA_RF_INT_OE,
    REG_FPGA0_XAB_RF_SW_CTRL,
    REG_FPGA0_XB_HSSI_PARM1,
    REG_FPGA0_XB_RF_INT_OE,
    REG_FPGA0_XCD_RF_SW_CTRL,
    REG_FPGA0_XCD_SWITCH_CTRL,
    REG_GPIO_MUXCFG,
    REG_IQK_AGC_PTS,
    REG_IQK_AGC_RSP,
    REG_OFDM0_AGC_RSSI_TABLE,
    REG_OFDM0_ENERGY_CCA_THRES,
    REG_OFDM0_RX_IQ_EXT_ANTA,
    REG_OFDM0_TR_MUX_PAR,
    REG_OFDM0_TRX_PATH_ENABLE,
    REG_OFDM0_XA_RX_IQ_IMBALANCE,
    REG_OFDM0_XA_TX_IQ_IMBALANCE,
    REG_OFDM0_XB_RX_IQ_IMBALANCE,
    REG_OFDM0_XB_TX_IQ_IMBALANCE,
    REG_OFDM0_XC_TX_AFE,
    REG_OFDM0_XD_TX_AFE,
    REG_PMPD_ANAEN,
    REG_RX_CCK,
    REG_RX_IQK,
    REG_RX_IQK_PI_A,
    REG_RX_IQK_TONE_A,
    REG_RX_OFDM,
    REG_RX_POWER_AFTER_IQK_A_2,
    REG_RX_POWER_BEFORE_IQK_A_2,
    REG_RX_TO_RX,
    REG_RX_WAIT_CCA,
    REG_RX_WAIT_RIFS,
    REG_SLEEP,
    REG_STANDBY,
    REG_TX_CCK_BBON,
    REG_TX_CCK_RFON,
    REG_TX_IQK,
    REG_TX_IQK_PI_A,
    REG_TX_IQK_TONE_A,
    REG_TX_OFDM_BBON,
    REG_TX_OFDM_RFON,
    REG_TX_POWER_AFTER_IQK_A,
    REG_TX_POWER_BEFORE_IQK_A,
    REG_TX_TO_RX,
    REG_TX_TO_TX,
    REG_TXPAUSE,
    RF6052_REG_RCK_OS,
    RF6052_REG_TXPA_G1,
    RF6052_REG_TXPA_G2,
    RF6052_REG_WE_LUT,
)
from .phy import RF_A, write_rfreg
from .transport import RTL8188EUSTransport

logger = logging.getLogger(__name__)

# ADDA / MAC / BB register sets backed up + restored around IQK (rtl8xxxu.h:904-906;
# orders mirror rtl8188eu_phy_iqcalibrate's static arrays, 8188e.c:757-776).
ADDA_REGS = (
    REG_FPGA0_XCD_SWITCH_CTRL, REG_BLUETOOTH, REG_RX_WAIT_CCA, REG_TX_CCK_RFON,
    REG_TX_CCK_BBON, REG_TX_OFDM_RFON, REG_TX_OFDM_BBON, REG_TX_TO_RX,
    REG_TX_TO_TX, REG_RX_CCK, REG_RX_OFDM, REG_RX_WAIT_RIFS,
    REG_RX_TO_RX, REG_STANDBY, REG_SLEEP, REG_PMPD_ANAEN,
)
IQK_MAC_REGS = (REG_TXPAUSE, REG_BEACON_CTRL, REG_BEACON_CTRL_1, REG_GPIO_MUXCFG)
IQK_BB_REGS = (
    REG_OFDM0_TRX_PATH_ENABLE, REG_OFDM0_TR_MUX_PAR, REG_FPGA0_XCD_RF_SW_CTRL,
    REG_CONFIG_ANT_A, REG_CONFIG_ANT_B, REG_FPGA0_XAB_RF_SW_CTRL,
    REG_FPGA0_XA_RF_INT_OE, REG_FPGA0_XB_RF_INT_OE, REG_CCK0_AFE_SETTING,
)


@dataclass
class IqkBackup:
    """Per-IQK saved register state (filled on iteration 0, restored on 1/2)."""
    adda: list[int] = field(default_factory=lambda: [0] * len(ADDA_REGS))
    mac: list[int] = field(default_factory=lambda: [0] * len(IQK_MAC_REGS))
    bb: list[int] = field(default_factory=lambda: [0] * len(IQK_BB_REGS))
    pi_enabled: bool = False


def _replace_bits(val: int, new: int, mask: int) -> int:
    """Mirror of the kernel ``u32p_replace_bits(&val, new, mask)`` — replace the
    ``mask`` field of ``val`` with ``new`` (shifted into the mask's low bit)."""
    shift = (mask & -mask).bit_length() - 1
    return ((val & ~mask) | ((new << shift) & mask)) & 0xFFFFFFFF


# ---- shared gen1 backup/restore helpers (core.c) --------------------


def save_regs(t: RTL8188EUSTransport, regs, backup) -> None:
    """`rtl8xxxu_save_regs` (core.c:3037) — read32 each reg into backup."""
    for i, reg in enumerate(regs):
        backup[i] = t.read32(reg)


def restore_regs(t: RTL8188EUSTransport, regs, backup) -> None:
    """`rtl8xxxu_restore_regs` (core.c:3046)."""
    for i, reg in enumerate(regs):
        t.write32(reg, backup[i])


def save_mac_regs(t: RTL8188EUSTransport, regs, backup) -> None:
    """`rtl8xxxu_save_mac_regs` (core.c:3015) — read8 for all but the last, read32 last."""
    for i in range(len(regs) - 1):
        backup[i] = t.read8(regs[i])
    backup[-1] = t.read32(regs[-1])


def restore_mac_regs(t: RTL8188EUSTransport, regs, backup) -> None:
    """`rtl8xxxu_restore_mac_regs` (core.c:3026)."""
    for i in range(len(regs) - 1):
        t.write8(regs[i], backup[i])
    t.write32(regs[-1], backup[-1])


def path_adda_on(t: RTL8188EUSTransport, regs) -> None:
    """`rtl8xxxu_path_adda_on` (core.c:3056), 1T path (8188e is 1T1R)."""
    t.write32(regs[0], ADDA_1T_INIT)
    for reg in regs[1:]:
        t.write32(reg, ADDA_1T_PATH_ON)


def mac_calibration(t: RTL8188EUSTransport, regs, backup) -> None:
    """`rtl8xxxu_mac_calibration` (core.c:3076)."""
    t.write8(regs[0], 0x3F)
    for i in range(1, len(regs) - 1):
        t.write8(regs[i], backup[i] & ~(1 << 3) & 0xFF)
    t.write8(regs[-1], backup[-1] & ~(1 << 5) & 0xFF)


# ---- 8188e path-A IQK ------------------------------------------------


def iqk_path_a(t: RTL8188EUSTransport) -> int:
    """Port of `rtl8188eu_iqk_path_a` (8188e.c:610) — one TX-IQK shot, path A."""
    t.write32(REG_TX_IQK_TONE_A, 0x10008C1C)
    t.write32(REG_RX_IQK_TONE_A, 0x30008C1C)
    t.write32(REG_TX_IQK_PI_A, 0x8214032A)
    t.write32(REG_RX_IQK_PI_A, 0x28160000)
    t.write32(REG_IQK_AGC_RSP, 0x00462911)
    t.write32(REG_IQK_AGC_PTS, 0xF9000000)
    t.write32(REG_IQK_AGC_PTS, 0xF8000000)
    time.sleep(0.010)
    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = t.read32(REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = t.read32(REG_TX_POWER_AFTER_IQK_A)
    result = 0
    if (not (reg_eac & (1 << 28))
            and (reg_e94 & 0x03FF0000) != 0x01420000
            and (reg_e9c & 0x03FF0000) != 0x00420000):
        result |= 0x01
    return result


def rx_iqk_path_a(t: RTL8188EUSTransport) -> int:
    """Port of `rtl8188eu_rx_iqk_path_a` (8188e.c:644) — TX then RX IQK shot, path A."""
    # Leave IQK mode
    val32 = _replace_bits(t.read32(REG_FPGA0_IQK), 0, 0xFFFFFF00)
    t.write32(REG_FPGA0_IQK, val32)

    # Enable path A PA in TX IQK mode
    write_rfreg(t, RF_A, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_A, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G2, 0xF117B)

    # Enter IQK mode
    val32 = _replace_bits(t.read32(REG_FPGA0_IQK), 0x808000, 0xFFFFFF00)
    t.write32(REG_FPGA0_IQK, val32)

    # TX IQK setting
    t.write32(REG_TX_IQK, 0x01007C00)
    t.write32(REG_RX_IQK, 0x81004800)

    # path-A IQK setting
    t.write32(REG_TX_IQK_TONE_A, 0x10008C1C)
    t.write32(REG_RX_IQK_TONE_A, 0x30008C1C)
    t.write32(REG_TX_IQK_PI_A, 0x82160804)
    t.write32(REG_RX_IQK_PI_A, 0x28160000)

    # LO calibration + one shot
    t.write32(REG_IQK_AGC_RSP, 0x0046A911)
    t.write32(REG_IQK_AGC_PTS, 0xF9000000)
    t.write32(REG_IQK_AGC_PTS, 0xF8000000)
    time.sleep(0.010)

    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = t.read32(REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = t.read32(REG_TX_POWER_AFTER_IQK_A)

    result = 0
    if (not (reg_eac & (1 << 28))
            and (reg_e94 & 0x03FF0000) != 0x01420000
            and (reg_e9c & 0x03FF0000) != 0x00420000):
        result |= 0x01
    else:
        return result  # goto out

    val32 = 0x80007C00 | (reg_e94 & 0x03FF0000) | ((reg_e9c >> 16) & 0x03FF)
    t.write32(REG_TX_IQK, val32)

    # Modify RX IQK mode table
    val32 = _replace_bits(t.read32(REG_FPGA0_IQK), 0, 0xFFFFFF00)
    t.write32(REG_FPGA0_IQK, val32)

    write_rfreg(t, RF_A, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_A, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G2, 0xF7FFA)

    # Enter IQK mode
    val32 = _replace_bits(t.read32(REG_FPGA0_IQK), 0x808000, 0xFFFFFF00)
    t.write32(REG_FPGA0_IQK, val32)

    # IQK setting
    t.write32(REG_RX_IQK, 0x01004800)

    # Path A IQK setting
    t.write32(REG_TX_IQK_TONE_A, 0x30008C1C)
    t.write32(REG_RX_IQK_TONE_A, 0x10008C1C)
    t.write32(REG_TX_IQK_PI_A, 0x82160C05)
    t.write32(REG_RX_IQK_PI_A, 0x28160C05)

    # LO calibration + one shot
    t.write32(REG_IQK_AGC_RSP, 0x0046A911)
    t.write32(REG_IQK_AGC_PTS, 0xF9000000)
    t.write32(REG_IQK_AGC_PTS, 0xF8000000)
    time.sleep(0.010)

    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_ea4 = t.read32(REG_RX_POWER_BEFORE_IQK_A_2)

    if (not (reg_eac & (1 << 27))
            and (reg_ea4 & 0x03FF0000) != 0x01320000
            and (reg_eac & 0x03FF0000) != 0x00360000):
        result |= 0x02
    else:
        logger.debug("Path A RX IQK failed")
    return result


def phy_iqcalibrate(t: RTL8188EUSTransport, result, t_idx: int, backup: IqkBackup) -> None:
    """Port of `rtl8188eu_phy_iqcalibrate` (8188e.c:750) — one IQK iteration.

    ``result`` is a 4x8 int matrix; this fills row ``t_idx`` columns 0-3 (TX/RX
    before/after power). On iteration 0 it saves ADDA/MAC/BB state into ``backup``
    and returns before restoring; iterations 1/2 restore at the end.
    """
    retry = 2

    if t_idx == 0:
        save_regs(t, ADDA_REGS, backup.adda)
        save_mac_regs(t, IQK_MAC_REGS, backup.mac)
        save_regs(t, IQK_BB_REGS, backup.bb)

    path_adda_on(t, ADDA_REGS)

    if t_idx == 0:
        backup.pi_enabled = bool(t.read32(REG_FPGA0_XA_HSSI_PARM1) & FPGA0_HSSI_PARM1_PI)

    if not backup.pi_enabled:
        # Switch BB to PI mode to do IQ Calibration.
        t.write32(REG_FPGA0_XA_HSSI_PARM1, 0x01000100)
        t.write32(REG_FPGA0_XB_HSSI_PARM1, 0x01000100)

    mac_calibration(t, IQK_MAC_REGS, backup.mac)

    val32 = _replace_bits(t.read32(REG_CCK0_AFE_SETTING), 0xF, 0x0F000000)
    t.write32(REG_CCK0_AFE_SETTING, val32)

    t.write32(REG_OFDM0_TRX_PATH_ENABLE, 0x03A05600)
    t.write32(REG_OFDM0_TR_MUX_PAR, 0x000800E4)
    t.write32(REG_FPGA0_XCD_RF_SW_CTRL, 0x22204000)

    # no_pape is 0 for 8188e -> always take this branch
    val32 = t.read32(REG_FPGA0_XAB_RF_SW_CTRL)
    val32 |= FPGA0_RF_PAPE | (FPGA0_RF_PAPE << FPGA0_RF_BD_CTRL_SHIFT)
    t.write32(REG_FPGA0_XAB_RF_SW_CTRL, val32)

    val32 = t.read32(REG_FPGA0_XA_RF_INT_OE) & ~(1 << 10)
    t.write32(REG_FPGA0_XA_RF_INT_OE, val32)
    val32 = t.read32(REG_FPGA0_XB_RF_INT_OE) & ~(1 << 10)
    t.write32(REG_FPGA0_XB_RF_INT_OE, val32)

    # Page B init
    t.write32(REG_CONFIG_ANT_A, 0x0F600000)

    # IQ calibration setting
    val32 = _replace_bits(t.read32(REG_FPGA0_IQK), 0x808000, 0xFFFFFF00)
    t.write32(REG_FPGA0_IQK, val32)
    t.write32(REG_TX_IQK, 0x01007C00)
    t.write32(REG_RX_IQK, 0x81004800)

    path_a_ok = 0
    for _ in range(retry):
        path_a_ok = iqk_path_a(t)
        if path_a_ok == 0x01:
            result[t_idx][0] = (t.read32(REG_TX_POWER_BEFORE_IQK_A) >> 16) & 0x3FF
            result[t_idx][1] = (t.read32(REG_TX_POWER_AFTER_IQK_A) >> 16) & 0x3FF
            break
    if not path_a_ok:
        logger.debug("Path A TX IQK failed")

    path_a_ok = 0
    for _ in range(retry):
        path_a_ok = rx_iqk_path_a(t)
        if path_a_ok == 0x03:
            result[t_idx][2] = (t.read32(REG_RX_POWER_BEFORE_IQK_A_2) >> 16) & 0x3FF
            result[t_idx][3] = (t.read32(REG_RX_POWER_AFTER_IQK_A_2) >> 16) & 0x3FF
            break
    if not path_a_ok:
        logger.debug("Path A RX IQK failed")

    # Back to BB mode, load original value
    val32 = _replace_bits(t.read32(REG_FPGA0_IQK), 0, 0xFFFFFF00)
    t.write32(REG_FPGA0_IQK, val32)

    if t_idx == 0:
        return

    if not backup.pi_enabled:
        # Switch back BB to SI mode after finishing IQ Calibration.
        t.write32(REG_FPGA0_XA_HSSI_PARM1, 0x01000000)
        t.write32(REG_FPGA0_XB_HSSI_PARM1, 0x01000000)

    restore_regs(t, ADDA_REGS, backup.adda)
    restore_mac_regs(t, IQK_MAC_REGS, backup.mac)
    restore_regs(t, IQK_BB_REGS, backup.bb)

    # Restore RX initial gain
    t.write32(REG_FPGA0_XA_LSSI_PARM, 0x00032ED3)

    # Load 0xe30 IQC default value
    t.write32(REG_TX_IQK_TONE_A, 0x01008C00)
    t.write32(REG_RX_IQK_TONE_A, 0x01008C00)


# BB registers saved into bb_recovery_backup at the tail of phy_iq_calibrate
# (rtl8xxxu_iqk_phy_iq_bb_reg, core.c:599-609).
IQK_PHY_IQ_BB_REG = (
    REG_OFDM0_XA_RX_IQ_IMBALANCE, REG_OFDM0_XB_RX_IQ_IMBALANCE, REG_OFDM0_ENERGY_CCA_THRES,
    REG_OFDM0_AGC_RSSI_TABLE, REG_OFDM0_XA_TX_IQ_IMBALANCE, REG_OFDM0_XB_TX_IQ_IMBALANCE,
    REG_OFDM0_XC_TX_AFE, REG_OFDM0_XD_TX_AFE, REG_OFDM0_RX_IQ_EXT_ANTA,
)

_MAX_TOLERANCE = 5


def fill_iqk_matrix_a(t: RTL8188EUSTransport, iqk_ok: bool, result, candidate, tx_only) -> None:
    """Port of `rtl8xxxu_fill_iqk_matrix_a` (core.c:2716) — apply the path-A IQK result
    to the OFDM TX/RX imbalance + CCA-threshold registers. 10-bit results are sign-extended
    and multiplied against the old TX-imbalance value (u32 wrap, matching the kernel)."""
    if not iqk_ok:
        return

    oldval = t.read32(REG_OFDM0_XA_TX_IQ_IMBALANCE) >> 22

    x = result[candidate][0]
    if x & 0x200:
        x |= 0xFFFFFC00
    tx0_a = ((x * oldval) & 0xFFFFFFFF) >> 8

    val32 = (t.read32(REG_OFDM0_XA_TX_IQ_IMBALANCE) & ~0x3FF) | tx0_a
    t.write32(REG_OFDM0_XA_TX_IQ_IMBALANCE, val32 & 0xFFFFFFFF)

    val32 = t.read32(REG_OFDM0_ENERGY_CCA_THRES) & ~(1 << 31)
    if ((x * oldval) >> 7) & 0x1:
        val32 |= (1 << 31)
    t.write32(REG_OFDM0_ENERGY_CCA_THRES, val32 & 0xFFFFFFFF)

    y = result[candidate][1]
    if y & 0x200:
        y |= 0xFFFFFC00
    tx0_c = ((y * oldval) & 0xFFFFFFFF) >> 8

    val32 = t.read32(REG_OFDM0_XC_TX_AFE) & ~0xF0000000
    val32 |= ((tx0_c & 0x3C0) >> 6) << 28
    t.write32(REG_OFDM0_XC_TX_AFE, val32 & 0xFFFFFFFF)

    val32 = t.read32(REG_OFDM0_XA_TX_IQ_IMBALANCE) & ~0x003F0000
    val32 |= (tx0_c & 0x3F) << 16
    t.write32(REG_OFDM0_XA_TX_IQ_IMBALANCE, val32 & 0xFFFFFFFF)

    val32 = t.read32(REG_OFDM0_ENERGY_CCA_THRES) & ~(1 << 29)
    if ((y * oldval) >> 7) & 0x1:
        val32 |= (1 << 29)
    t.write32(REG_OFDM0_ENERGY_CCA_THRES, val32 & 0xFFFFFFFF)

    if tx_only:
        return

    reg = result[candidate][2]
    val32 = (t.read32(REG_OFDM0_XA_RX_IQ_IMBALANCE) & ~0x3FF) | (reg & 0x3FF)
    t.write32(REG_OFDM0_XA_RX_IQ_IMBALANCE, val32 & 0xFFFFFFFF)

    reg = result[candidate][3] & 0x3F
    val32 = t.read32(REG_OFDM0_XA_RX_IQ_IMBALANCE) & ~0xFC00
    val32 |= (reg << 10) & 0xFC00
    t.write32(REG_OFDM0_XA_RX_IQ_IMBALANCE, val32 & 0xFFFFFFFF)

    reg = (result[candidate][3] >> 6) & 0xF
    val32 = t.read32(REG_OFDM0_RX_IQ_EXT_ANTA) & ~0xF0000000
    val32 |= reg << 28
    t.write32(REG_OFDM0_RX_IQ_EXT_ANTA, val32 & 0xFFFFFFFF)


def simularity_compare(result, c1: int, c2: int, tx_paths: int = 1) -> bool:
    """Port of `rtl8xxxu_simularity_compare` (gen1, core.c:2876). Pure computation on the
    result matrix — picks/merges the converged candidate into ``result[3]``. No device I/O."""
    bound = 8 if tx_paths > 1 else 4
    candidate = [-1, -1]
    simubitmap = 0

    for i in range(bound):
        diff = abs(result[c1][i] - result[c2][i])
        if diff > _MAX_TOLERANCE:
            if (i == 2 or i == 6) and not simubitmap:
                if result[c1][i] + result[c1][i + 1] == 0:
                    candidate[i // 4] = c2
                elif result[c2][i] + result[c2][i + 1] == 0:
                    candidate[i // 4] = c1
                else:
                    simubitmap |= (1 << i)
            else:
                simubitmap |= (1 << i)

    if simubitmap == 0:
        retval = True
        for i in range(bound // 4):
            if candidate[i] >= 0:
                for j in range(i * 4, (i + 1) * 4 - 2):
                    result[3][j] = result[candidate[i]][j]
                retval = False
        return retval
    if not (simubitmap & 0x0F):
        for i in range(4):
            result[3][i] = result[c1][i]
    elif not (simubitmap & 0xF0) and tx_paths > 1:
        for i in range(4, 8):
            result[3][i] = result[c1][i]
    return False


def phy_iq_calibrate(t: RTL8188EUSTransport) -> None:
    """Port of `rtl8188eu_phy_iq_calibrate` (8188e.c:906) — run up to 3 IQK iterations,
    pick the converged candidate via similarity compare, apply it, then snapshot the OFDM
    IQ-correction registers into the recovery backup."""
    result = [[0] * 8 for _ in range(4)]
    result[3][0] = result[3][2] = result[3][4] = result[3][6] = 0x100
    candidate = -1
    path_a_ok = False
    backup = IqkBackup()

    for i in range(3):
        phy_iqcalibrate(t, result, i, backup)
        if i == 1 and simularity_compare(result, 0, 1):
            candidate = 0
            break
        if i == 2:
            if simularity_compare(result, 0, 2):
                candidate = 0
                break
            candidate = 1 if simularity_compare(result, 1, 2) else 3

    if candidate >= 0:
        reg_e94 = result[candidate][0]
        reg_ea4 = result[candidate][2]
        path_a_ok = True
    else:
        reg_e94 = 0x100
        reg_ea4 = 0

    if reg_e94 and candidate >= 0:
        fill_iqk_matrix_a(t, path_a_ok, result, candidate, reg_ea4 == 0)

    bb_recovery = [0] * len(IQK_PHY_IQ_BB_REG)
    save_regs(t, IQK_PHY_IQ_BB_REG, bb_recovery)
