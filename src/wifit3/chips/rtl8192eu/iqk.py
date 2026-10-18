"""RTL8192EUS IQ + LC calibration (2T2R, paths A and B).

Cleanroom port of the 8192e IQK + the shared ``rtl8xxxu`` IQK helpers:

* ``rtl8192eu_phy_iq_calibrate``  — ``8192e.c:1236`` (3-iteration wrapper + gen2 similarity)
* ``rtl8192eu_phy_iqcalibrate``   — ``8192e.c:1050`` (one iteration: backup → cal → restore)
* ``rtl8192eu_iqk_path_a``        — ``8192e.c:693``  (TX IQK path A)
* ``rtl8192eu_rx_iqk_path_a``     — ``8192e.c:743``  (RX IQK path A)
* ``rtl8192eu_iqk_path_b``        — ``8192e.c:870``  (TX IQK path B)
* ``rtl8192eu_rx_iqk_path_b``     — ``8192e.c:919``  (RX IQK path B)
* ``rtl8xxxu_save_regs`` / ``save_mac_regs`` / ``restore_regs`` / ``restore_mac_regs``
                                   — ``core.c:3037 / 3015 / 3046 / 3026``
* ``rtl8xxxu_path_adda_on``       — ``core.c:3056``
* ``rtl8xxxu_mac_calibration``    — ``core.c:3076``
* ``rtl8xxxu_gen2_simularity_compare`` — ``core.c:2930``
* ``rtl8xxxu_fill_iqk_matrix_a`` / ``_b`` — ``core.c:2716 / 2793``
* ``rtl8723a_phy_lc_calibrate``   — ``core.c:3498``

IQK is a closed-loop calibration: write a trigger, the chip measures the path's TX/RX
imbalance, the driver reads the result registers and either accepts or retries, then a final
pass fills the OFDM correction matrix. The RTL8192EU is 2T2R (``rf_paths = 2``, ``8192e.c:490``),
so both paths are exercised — path A first, then path A is parked in standby while path B runs
(``8192e.c:1156-1198``). Because every measurement-read and correction-write is on the wire,
this replays against the cold-boot capture: ``verify_pcap`` serves the recorded reads, the
ported algorithm reproduces the recorded writes.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .constants import (
    FPGA0_RF_BD_CTRL_SHIFT,
    FPGA0_RF_PAPE,
    OFDM_LSTF_MASK,
    REG_BEACON_CTRL,
    REG_BEACON_CTRL_1,
    REG_BLUETOOTH,
    REG_CCK0_AFE_SETTING,
    REG_CONFIG_ANT_A,
    REG_CONFIG_ANT_B,
    REG_FPGA0_IQK,
    REG_FPGA0_XA_RF_INT_OE,
    REG_FPGA0_XAB_RF_SW_CTRL,
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
    REG_OFDM0_XA_AGC_CORE1,
    REG_OFDM0_XA_RX_IQ_IMBALANCE,
    REG_OFDM0_XA_TX_IQ_IMBALANCE,
    REG_OFDM0_XB_AGC_CORE1,
    REG_OFDM0_XB_RX_IQ_IMBALANCE,
    REG_OFDM0_XB_TX_IQ_IMBALANCE,
    REG_OFDM0_XC_TX_AFE,
    REG_OFDM0_XD_TX_AFE,
    REG_OFDM1_LSTF,
    REG_PMPD_ANAEN,
    REG_RX_CCK,
    REG_RX_IQK,
    REG_RX_IQK_PI_A,
    REG_RX_IQK_PI_B,
    REG_RX_IQK_TONE_A,
    REG_RX_IQK_TONE_B,
    REG_RX_OFDM,
    REG_RX_POWER_AFTER_IQK_A_2,
    REG_RX_POWER_AFTER_IQK_B_2,
    REG_RX_POWER_BEFORE_IQK_A_2,
    REG_RX_POWER_BEFORE_IQK_B_2,
    REG_RX_TO_RX,
    REG_RX_WAIT_CCA,
    REG_RX_WAIT_RIFS,
    REG_SLEEP,
    REG_STANDBY,
    REG_TX_CCK_BBON,
    REG_TX_CCK_RFON,
    REG_TX_IQK,
    REG_TX_IQK_PI_A,
    REG_TX_IQK_PI_B,
    REG_TX_IQK_TONE_A,
    REG_TX_IQK_TONE_B,
    REG_TX_OFDM_BBON,
    REG_TX_OFDM_RFON,
    REG_TX_POWER_AFTER_IQK_A,
    REG_TX_POWER_AFTER_IQK_B,
    REG_TX_POWER_BEFORE_IQK_A,
    REG_TX_POWER_BEFORE_IQK_B,
    REG_TX_TO_RX,
    REG_TX_TO_TX,
    REG_TXPAUSE,
    RF6052_REG_AC,
    RF6052_REG_GAIN_CCA,
    RF6052_REG_MODE_AG,
    RF6052_REG_PAD_TXG,
    RF6052_REG_RCK_OS,
    RF6052_REG_TXPA_G1,
    RF6052_REG_TXPA_G2,
    RF6052_REG_WE_LUT,
)
from .chan import read_rfreg
from .phy import RF_A, RF_B, write_rfreg
from .transport import RTL8192EUTransport

logger = logging.getLogger(__name__)

# ADDA / MAC / BB register sets backed up + restored around IQK (rtl8xxxu.h:904-906;
# orders mirror rtl8192eu_phy_iqcalibrate's static arrays, 8192e.c:1057-1075).
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

# ADDA 2T path-on words (rtl8192eu fops ``adda_2t_path_on_a`` / ``_b``, 8192e.c:1759-1760;
# both identical). These are not yet exported by constants.py (which still carries the 8188e
# 1T values) — see report.
ADDA_2T_PATH_ON_A = 0x0FC01616
ADDA_2T_PATH_ON_B = 0x0FC01616

# ``rtl8192eu_identify_chip`` sets ``tx_paths = 2`` for the 8192EU (8192e.c:487; the 1T2R
# 8191EU sets 1) and ``rf_paths = 2`` (8192e.c:490). This port targets the 8192EU.
TX_PATHS = 2


@dataclass
class IqkBackup:
    """Per-IQK saved register state (filled on iteration 0, restored on iterations 1/2)."""
    adda: list[int] = field(default_factory=lambda: [0] * len(ADDA_REGS))
    mac: list[int] = field(default_factory=lambda: [0] * len(IQK_MAC_REGS))
    bb: list[int] = field(default_factory=lambda: [0] * len(IQK_BB_REGS))


# ---- shared gen1 backup/restore helpers (core.c) --------------------


def save_regs(t: RTL8192EUTransport, regs, backup) -> None:
    """`rtl8xxxu_save_regs` (core.c:3037) — read32 each reg into backup."""
    for i, reg in enumerate(regs):
        backup[i] = t.read32(reg)


def restore_regs(t: RTL8192EUTransport, regs, backup) -> None:
    """`rtl8xxxu_restore_regs` (core.c:3046)."""
    for i, reg in enumerate(regs):
        t.write32(reg, backup[i])


def save_mac_regs(t: RTL8192EUTransport, regs, backup) -> None:
    """`rtl8xxxu_save_mac_regs` (core.c:3015) — read8 for all but the last, read32 last."""
    for i in range(len(regs) - 1):
        backup[i] = t.read8(regs[i])
    backup[-1] = t.read32(regs[-1])


def restore_mac_regs(t: RTL8192EUTransport, regs, backup) -> None:
    """`rtl8xxxu_restore_mac_regs` (core.c:3026)."""
    for i in range(len(regs) - 1):
        t.write8(regs[i], backup[i])
    t.write32(regs[-1], backup[-1])


def path_adda_on(t: RTL8192EUTransport, regs, path_a: bool = True) -> None:
    """`rtl8xxxu_path_adda_on` (core.c:3056) — turn the requested path's ADDA on.

    8192eu has ``tx_paths == 2``, so the 2T branch runs: ``regs[0]`` and every ``regs[1:]``
    take the same path-on word (``path_a_on ? adda_2t_path_on_a : adda_2t_path_on_b``,
    core.c:3066-3073). Both 8192eu words are identical (8192e.c:1759-1760).
    """
    path_on = ADDA_2T_PATH_ON_A if path_a else ADDA_2T_PATH_ON_B
    t.write32(regs[0], path_on)
    for reg in regs[1:]:
        t.write32(reg, path_on)


def mac_calibration(t: RTL8192EUTransport, regs, backup) -> None:
    """`rtl8xxxu_mac_calibration` (core.c:3076)."""
    t.write8(regs[0], 0x3F)
    for i in range(1, len(regs) - 1):
        t.write8(regs[i], backup[i] & ~(1 << 3) & 0xFF)
    t.write8(regs[-1], backup[-1] & ~(1 << 5) & 0xFF)


# ---- 8192e path-A IQK ------------------------------------------------


def iqk_path_a(t: RTL8192EUTransport) -> int:
    """Port of `rtl8192eu_iqk_path_a` (8192e.c:693) — one TX-IQK shot, path A."""
    # TX IQK; PA/PAD controlled by 0x0.
    t.write32(REG_FPGA0_IQK, 0x00000000)
    write_rfreg(t, RF_A, RF6052_REG_GAIN_CCA, 0x00180)
    write_rfreg(t, RF_A, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_A, RF6052_REG_RCK_OS, 0x20000)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G2, 0x07F77)

    t.write32(REG_FPGA0_IQK, 0x80800000)

    # Path A IQK setting.
    t.write32(REG_TX_IQK_TONE_A, 0x18008C1C)
    t.write32(REG_RX_IQK_TONE_A, 0x38008C1C)
    t.write32(REG_TX_IQK_TONE_B, 0x38008C1C)
    t.write32(REG_RX_IQK_TONE_B, 0x38008C1C)
    t.write32(REG_TX_IQK_PI_A, 0x82140303)
    t.write32(REG_RX_IQK_PI_A, 0x68160000)

    # LO calibration setting.
    t.write32(REG_IQK_AGC_RSP, 0x00462911)

    # One shot, path A LOK & IQK.
    t.write32(REG_IQK_AGC_PTS, 0xF9000000)
    t.write32(REG_IQK_AGC_PTS, 0xF8000000)
    time.sleep(0.010)

    # Check failed.
    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = t.read32(REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = t.read32(REG_TX_POWER_AFTER_IQK_A)

    result = 0
    if (not (reg_eac & (1 << 28))
            and (reg_e94 & 0x03FF0000) != 0x01420000
            and (reg_e9c & 0x03FF0000) != 0x00420000):
        result |= 0x01
    return result


def rx_iqk_path_a(t: RTL8192EUTransport) -> int:
    """Port of `rtl8192eu_rx_iqk_path_a` (8192e.c:743) — TX then RX IQK shot, path A.

    Both RF paths are preloaded here (path A then path B) even though only path A's result is
    used; path B's table entries are set for the two RF6052 chips sharing the calibration
    sequence (8192e.c:752-760)."""
    # Leave IQK mode.
    t.write32(REG_FPGA0_IQK, 0x00000000)

    # Enable path A PA in TX IQK mode (path B table too).
    write_rfreg(t, RF_A, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_A, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G2, 0xF1173)

    write_rfreg(t, RF_B, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_B, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, RF_B, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_B, RF6052_REG_TXPA_G2, 0xF1173)

    # PA/PAD control by 0x56, and set = 0x0.
    write_rfreg(t, RF_A, RF6052_REG_GAIN_CCA, 0x00980)
    write_rfreg(t, RF_A, RF6052_REG_PAD_TXG, 0x511E0)

    # Enter IQK mode.
    t.write32(REG_FPGA0_IQK, 0x80800000)

    # TX IQK setting.
    t.write32(REG_TX_IQK, 0x01007C00)
    t.write32(REG_RX_IQK, 0x01004800)

    # Path A IQK setting.
    t.write32(REG_TX_IQK_TONE_A, 0x18008C1C)
    t.write32(REG_RX_IQK_TONE_A, 0x38008C1C)
    t.write32(REG_TX_IQK_TONE_B, 0x38008C1C)
    t.write32(REG_RX_IQK_TONE_B, 0x38008C1C)
    t.write32(REG_TX_IQK_PI_A, 0x8216031F)
    t.write32(REG_RX_IQK_PI_A, 0x6816031F)

    # LO calibration setting.
    t.write32(REG_IQK_AGC_RSP, 0x0046A911)

    # One shot, path A LOK & IQK.
    t.write32(REG_IQK_AGC_PTS, 0xF9000000)
    t.write32(REG_IQK_AGC_PTS, 0xF8000000)
    time.sleep(0.010)

    # Check failed.
    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_e94 = t.read32(REG_TX_POWER_BEFORE_IQK_A)
    reg_e9c = t.read32(REG_TX_POWER_AFTER_IQK_A)

    result = 0
    if (not (reg_eac & (1 << 28))
            and (reg_e94 & 0x03FF0000) != 0x01420000
            and (reg_e9c & 0x03FF0000) != 0x00420000):
        result |= 0x01
    else:
        # PA/PAD controlled by 0x0.
        t.write32(REG_FPGA0_IQK, 0x00000000)
        write_rfreg(t, RF_A, RF6052_REG_GAIN_CCA, 0x180)
        return result  # goto out

    val32 = 0x80007C00 | (reg_e94 & 0x03FF0000) | ((reg_e9c >> 16) & 0x03FF)
    t.write32(REG_TX_IQK, val32)

    # Modify RX IQK mode table.
    t.write32(REG_FPGA0_IQK, 0x00000000)

    write_rfreg(t, RF_A, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_A, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G2, 0xF7FF2)

    write_rfreg(t, RF_B, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_B, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, RF_B, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_B, RF6052_REG_TXPA_G2, 0xF7FF2)

    # PA/PAD control by 0x56, and set = 0x0.
    write_rfreg(t, RF_A, RF6052_REG_GAIN_CCA, 0x00980)
    write_rfreg(t, RF_A, RF6052_REG_PAD_TXG, 0x510E0)

    # Enter IQK mode.
    t.write32(REG_FPGA0_IQK, 0x80800000)

    # IQK setting.
    t.write32(REG_RX_IQK, 0x01004800)

    # Path A IQK setting.
    t.write32(REG_TX_IQK_TONE_A, 0x38008C1C)
    t.write32(REG_RX_IQK_TONE_A, 0x18008C1C)
    t.write32(REG_TX_IQK_TONE_B, 0x38008C1C)
    t.write32(REG_RX_IQK_TONE_B, 0x38008C1C)
    t.write32(REG_TX_IQK_PI_A, 0x821608FF)
    t.write32(REG_RX_IQK_PI_A, 0x281608FF)

    # LO calibration setting.
    t.write32(REG_IQK_AGC_RSP, 0x0046A891)

    # One shot, path A LOK & IQK.
    t.write32(REG_IQK_AGC_PTS, 0xF9000000)
    t.write32(REG_IQK_AGC_PTS, 0xF8000000)
    time.sleep(0.010)

    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_ea4 = t.read32(REG_RX_POWER_BEFORE_IQK_A_2)

    t.write32(REG_FPGA0_IQK, 0x00000000)
    write_rfreg(t, RF_A, RF6052_REG_GAIN_CCA, 0x180)

    if (not (reg_eac & (1 << 27))
            and (reg_ea4 & 0x03FF0000) != 0x01320000
            and (reg_eac & 0x03FF0000) != 0x00360000):
        result |= 0x02
    else:
        logger.debug("Path A RX IQK failed")
    return result


# ---- 8192e path-B IQK ------------------------------------------------


def iqk_path_b(t: RTL8192EUTransport) -> int:
    """Port of `rtl8192eu_iqk_path_b` (8192e.c:870) — one TX-IQK shot, path B."""
    # TX IQK; PA/PAD controlled by 0x0.
    t.write32(REG_FPGA0_IQK, 0x00000000)
    write_rfreg(t, RF_B, RF6052_REG_GAIN_CCA, 0x00180)
    write_rfreg(t, RF_B, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_B, RF6052_REG_RCK_OS, 0x20000)
    write_rfreg(t, RF_B, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_B, RF6052_REG_TXPA_G2, 0x07F77)

    t.write32(REG_FPGA0_IQK, 0x80800000)

    # Path B IQK setting.
    t.write32(REG_TX_IQK_TONE_A, 0x38008C1C)
    t.write32(REG_RX_IQK_TONE_A, 0x38008C1C)
    t.write32(REG_TX_IQK_TONE_B, 0x18008C1C)
    t.write32(REG_RX_IQK_TONE_B, 0x38008C1C)
    t.write32(REG_TX_IQK_PI_B, 0x82140303)
    t.write32(REG_RX_IQK_PI_B, 0x68160000)

    # LO calibration setting.
    t.write32(REG_IQK_AGC_RSP, 0x00462911)

    # One shot, path B LOK & IQK (PTT 0xfa = path B).
    t.write32(REG_IQK_AGC_PTS, 0xFA000000)
    t.write32(REG_IQK_AGC_PTS, 0xF8000000)
    time.sleep(0.001)  # mdelay(1) — note: 1 ms here, not the 10 ms of the path-A shot.

    # Check failed.
    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_eb4 = t.read32(REG_TX_POWER_BEFORE_IQK_B)
    reg_ebc = t.read32(REG_TX_POWER_AFTER_IQK_B)

    result = 0
    if (not (reg_eac & (1 << 31))
            and (reg_eb4 & 0x03FF0000) != 0x01420000
            and (reg_ebc & 0x03FF0000) != 0x00420000):
        result |= 0x01
    else:
        logger.debug("Path B IQK failed")
    return result


def rx_iqk_path_b(t: RTL8192EUTransport) -> int:
    """Port of `rtl8192eu_rx_iqk_path_b` (8192e.c:919) — TX then RX IQK shot, path B.

    Mirrors ``rx_iqk_path_a`` with RF_B/RF_A swapped and the path-B result registers."""
    # Leave IQK mode.
    t.write32(REG_FPGA0_IQK, 0x00000000)

    # Enable path B PA in TX IQK mode (path A table too).
    write_rfreg(t, RF_B, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_B, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, RF_B, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_B, RF6052_REG_TXPA_G2, 0xF1173)

    write_rfreg(t, RF_A, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_A, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G2, 0xF1173)

    # PA/PAD control by 0x56, and set = 0x0.
    write_rfreg(t, RF_B, RF6052_REG_GAIN_CCA, 0x00980)
    write_rfreg(t, RF_B, RF6052_REG_PAD_TXG, 0x511E0)

    # Enter IQK mode.
    t.write32(REG_FPGA0_IQK, 0x80800000)

    # TX IQK setting.
    t.write32(REG_TX_IQK, 0x01007C00)
    t.write32(REG_RX_IQK, 0x01004800)

    # Path B IQK setting.
    t.write32(REG_TX_IQK_TONE_A, 0x38008C1C)
    t.write32(REG_RX_IQK_TONE_A, 0x38008C1C)
    t.write32(REG_TX_IQK_TONE_B, 0x18008C1C)
    t.write32(REG_RX_IQK_TONE_B, 0x38008C1C)
    t.write32(REG_TX_IQK_PI_B, 0x8216031F)
    t.write32(REG_RX_IQK_PI_B, 0x6816031F)

    # LO calibration setting.
    t.write32(REG_IQK_AGC_RSP, 0x0046A911)

    # One shot, path B LOK & IQK.
    t.write32(REG_IQK_AGC_PTS, 0xFA000000)
    t.write32(REG_IQK_AGC_PTS, 0xF8000000)
    time.sleep(0.010)

    # Check failed.
    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_eb4 = t.read32(REG_TX_POWER_BEFORE_IQK_B)
    reg_ebc = t.read32(REG_TX_POWER_AFTER_IQK_B)

    result = 0
    if (not (reg_eac & (1 << 31))
            and (reg_eb4 & 0x03FF0000) != 0x01420000
            and (reg_ebc & 0x03FF0000) != 0x00420000):
        result |= 0x01
    else:
        # PA/PAD controlled by 0x0 (the vendor driver restores RF_A here, which the kernel
        # treats as a bug — it restores RF_B; 8192e.c:977-983).
        t.write32(REG_FPGA0_IQK, 0x00000000)
        write_rfreg(t, RF_B, RF6052_REG_GAIN_CCA, 0x180)
        return result  # goto out

    val32 = 0x80007C00 | (reg_eb4 & 0x03FF0000) | ((reg_ebc >> 16) & 0x03FF)
    t.write32(REG_TX_IQK, val32)

    # Modify RX IQK mode table.
    t.write32(REG_FPGA0_IQK, 0x00000000)

    write_rfreg(t, RF_B, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_B, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, RF_B, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_B, RF6052_REG_TXPA_G2, 0xF7FF2)

    write_rfreg(t, RF_A, RF6052_REG_WE_LUT, 0x800A0)
    write_rfreg(t, RF_A, RF6052_REG_RCK_OS, 0x30000)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G1, 0x0000F)
    write_rfreg(t, RF_A, RF6052_REG_TXPA_G2, 0xF7FF2)

    # PA/PAD control by 0x56, and set = 0x0.
    write_rfreg(t, RF_B, RF6052_REG_GAIN_CCA, 0x00980)
    write_rfreg(t, RF_B, RF6052_REG_PAD_TXG, 0x510E0)

    # Enter IQK mode.
    t.write32(REG_FPGA0_IQK, 0x80800000)

    # IQK setting.
    t.write32(REG_RX_IQK, 0x01004800)

    # Path B IQK setting.
    t.write32(REG_TX_IQK_TONE_A, 0x38008C1C)
    t.write32(REG_RX_IQK_TONE_A, 0x38008C1C)
    t.write32(REG_TX_IQK_TONE_B, 0x38008C1C)
    t.write32(REG_RX_IQK_TONE_B, 0x18008C1C)
    t.write32(REG_TX_IQK_PI_A, 0x821608FF)
    t.write32(REG_RX_IQK_PI_A, 0x281608FF)

    # LO calibration setting.
    t.write32(REG_IQK_AGC_RSP, 0x0046A891)

    # One shot, path B LOK & IQK.
    t.write32(REG_IQK_AGC_PTS, 0xFA000000)
    t.write32(REG_IQK_AGC_PTS, 0xF8000000)
    time.sleep(0.010)

    reg_eac = t.read32(REG_RX_POWER_AFTER_IQK_A_2)
    reg_ec4 = t.read32(REG_RX_POWER_BEFORE_IQK_B_2)
    reg_ecc = t.read32(REG_RX_POWER_AFTER_IQK_B_2)

    t.write32(REG_FPGA0_IQK, 0x00000000)
    write_rfreg(t, RF_B, RF6052_REG_GAIN_CCA, 0x180)

    if (not (reg_eac & (1 << 30))
            and (reg_ec4 & 0x03FF0000) != 0x01320000
            and (reg_ecc & 0x03FF0000) != 0x00360000):
        result |= 0x02
    else:
        logger.debug("Path B RX IQK failed")
    return result


def phy_iqcalibrate(t: RTL8192EUTransport, result, t_idx: int, backup: IqkBackup) -> None:
    """Port of `rtl8192eu_phy_iqcalibrate` (8192e.c:1050) — one IQK iteration.

    ``result`` is a 4x8 int matrix; this fills row ``t_idx`` columns 0-3 (path A) and 4-7
    (path B). On iteration 0 it saves ADDA/MAC/BB state into ``backup`` and returns before
    restoring; iterations 1/2 restore at the end. Unlike the 8188e, the 8192e never touches
    the BB PI/SI mode (no ``REG_FPGA0_*_HSSI_PARM1`` dance) and it *sets* BIT(10) of the RF
    INT_OE registers (8192e.c:1111-1116)."""
    retry = 2

    # Grab the per-path AGC values before anything else mutates them (8192e.c:1077-1078).
    xa_agc = t.read32(REG_OFDM0_XA_AGC_CORE1) & 0xFF
    xb_agc = t.read32(REG_OFDM0_XB_AGC_CORE1) & 0xFF

    if t_idx == 0:
        save_regs(t, ADDA_REGS, backup.adda)
        save_mac_regs(t, IQK_MAC_REGS, backup.mac)
        save_regs(t, IQK_BB_REGS, backup.bb)

    path_adda_on(t, ADDA_REGS)

    mac_calibration(t, IQK_MAC_REGS, backup.mac)

    val32 = t.read32(REG_CCK0_AFE_SETTING) | 0x0F000000
    t.write32(REG_CCK0_AFE_SETTING, val32 & 0xFFFFFFFF)

    t.write32(REG_OFDM0_TRX_PATH_ENABLE, 0x03A05600)
    t.write32(REG_OFDM0_TR_MUX_PAR, 0x000800E4)
    t.write32(REG_FPGA0_XCD_RF_SW_CTRL, 0x22208200)

    val32 = t.read32(REG_FPGA0_XAB_RF_SW_CTRL)
    val32 |= FPGA0_RF_PAPE | (FPGA0_RF_PAPE << FPGA0_RF_BD_CTRL_SHIFT)
    t.write32(REG_FPGA0_XAB_RF_SW_CTRL, val32 & 0xFFFFFFFF)

    val32 = t.read32(REG_FPGA0_XA_RF_INT_OE) | (1 << 10)
    t.write32(REG_FPGA0_XA_RF_INT_OE, val32 & 0xFFFFFFFF)
    val32 = t.read32(REG_FPGA0_XB_RF_INT_OE) | (1 << 10)
    t.write32(REG_FPGA0_XB_RF_INT_OE, val32 & 0xFFFFFFFF)

    # IQ calibration setting.
    t.write32(REG_FPGA0_IQK, 0x80800000)
    t.write32(REG_TX_IQK, 0x01007C00)
    t.write32(REG_RX_IQK, 0x01004800)

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

    # 2T2R (rf_paths = 2): park path A in standby, then turn path B ADDA on and run path B.
    t.write32(REG_FPGA0_IQK, 0x00000000)
    write_rfreg(t, RF_A, RF6052_REG_AC, 0x10000)
    t.write32(REG_FPGA0_IQK, 0x80800000)

    path_adda_on(t, ADDA_REGS, False)

    t.write32(REG_FPGA0_IQK, 0x80800000)
    t.write32(REG_TX_IQK, 0x01007C00)
    t.write32(REG_RX_IQK, 0x01004800)

    path_b_ok = 0
    for _ in range(retry):
        path_b_ok = iqk_path_b(t)
        if path_b_ok == 0x01:
            result[t_idx][4] = (t.read32(REG_TX_POWER_BEFORE_IQK_B) >> 16) & 0x3FF
            result[t_idx][5] = (t.read32(REG_TX_POWER_AFTER_IQK_B) >> 16) & 0x3FF
            break
    if not path_b_ok:
        logger.debug("Path B IQK failed")

    path_b_ok = 0
    for _ in range(retry):
        path_b_ok = rx_iqk_path_b(t)
        if path_b_ok == 0x03:
            result[t_idx][6] = (t.read32(REG_RX_POWER_BEFORE_IQK_B_2) >> 16) & 0x3FF
            result[t_idx][7] = (t.read32(REG_RX_POWER_AFTER_IQK_B_2) >> 16) & 0x3FF
            break
    if not path_b_ok:
        logger.debug("Path B RX IQK failed")

    # Back to BB mode, load original value.
    t.write32(REG_FPGA0_IQK, 0x00000000)

    if t_idx == 0:
        return

    # Reload ADDA power saving parameters.
    restore_regs(t, ADDA_REGS, backup.adda)

    # Reload MAC parameters.
    restore_mac_regs(t, IQK_MAC_REGS, backup.mac)

    # Reload BB parameters.
    restore_regs(t, IQK_BB_REGS, backup.bb)

    # Restore RX initial gain: pulse 0x50 then the saved value, path A then path B.
    val32 = t.read32(REG_OFDM0_XA_AGC_CORE1) & 0xFFFFFF00
    t.write32(REG_OFDM0_XA_AGC_CORE1, (val32 | 0x50) & 0xFFFFFFFF)
    t.write32(REG_OFDM0_XA_AGC_CORE1, (val32 | xa_agc) & 0xFFFFFFFF)

    val32 = t.read32(REG_OFDM0_XB_AGC_CORE1) & 0xFFFFFF00
    t.write32(REG_OFDM0_XB_AGC_CORE1, (val32 | 0x50) & 0xFFFFFFFF)
    t.write32(REG_OFDM0_XB_AGC_CORE1, (val32 | xb_agc) & 0xFFFFFFFF)

    # Load 0xe30 IQC default value.
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


def fill_iqk_matrix_a(t: RTL8192EUTransport, iqk_ok: bool, result, candidate, tx_only) -> None:
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


def fill_iqk_matrix_b(t: RTL8192EUTransport, iqk_ok: bool, result, candidate, tx_only) -> None:
    """Port of `rtl8xxxu_fill_iqk_matrix_b` (core.c:2793) — apply the path-B IQK result.

    Same shape as the path-A matrix with the XB/XD registers and the BIT(27)/BIT(25) CCA
    threshold flags. The RTL8192F-only ``REG_RXIQB_EXT`` branch (core.c:2864-2865) does not
    apply to the 8192E, so the RX extension lands in ``REG_OFDM0_AGC_RSSI_TABLE`` bits 15:12
    (core.c:2867-2870)."""
    if not iqk_ok:
        return

    oldval = t.read32(REG_OFDM0_XB_TX_IQ_IMBALANCE) >> 22

    x = result[candidate][4]
    if x & 0x200:
        x |= 0xFFFFFC00
    tx1_a = ((x * oldval) & 0xFFFFFFFF) >> 8

    val32 = (t.read32(REG_OFDM0_XB_TX_IQ_IMBALANCE) & ~0x3FF) | tx1_a
    t.write32(REG_OFDM0_XB_TX_IQ_IMBALANCE, val32 & 0xFFFFFFFF)

    val32 = t.read32(REG_OFDM0_ENERGY_CCA_THRES) & ~(1 << 27)
    if ((x * oldval) >> 7) & 0x1:
        val32 |= (1 << 27)
    t.write32(REG_OFDM0_ENERGY_CCA_THRES, val32 & 0xFFFFFFFF)

    y = result[candidate][5]
    if y & 0x200:
        y |= 0xFFFFFC00
    tx1_c = ((y * oldval) & 0xFFFFFFFF) >> 8

    val32 = t.read32(REG_OFDM0_XD_TX_AFE) & ~0xF0000000
    val32 |= ((tx1_c & 0x3C0) >> 6) << 28
    t.write32(REG_OFDM0_XD_TX_AFE, val32 & 0xFFFFFFFF)

    val32 = t.read32(REG_OFDM0_XB_TX_IQ_IMBALANCE) & ~0x003F0000
    val32 |= (tx1_c & 0x3F) << 16
    t.write32(REG_OFDM0_XB_TX_IQ_IMBALANCE, val32 & 0xFFFFFFFF)

    val32 = t.read32(REG_OFDM0_ENERGY_CCA_THRES) & ~(1 << 25)
    if ((y * oldval) >> 7) & 0x1:
        val32 |= (1 << 25)
    t.write32(REG_OFDM0_ENERGY_CCA_THRES, val32 & 0xFFFFFFFF)

    if tx_only:
        return

    reg = result[candidate][6]
    val32 = (t.read32(REG_OFDM0_XB_RX_IQ_IMBALANCE) & ~0x3FF) | (reg & 0x3FF)
    t.write32(REG_OFDM0_XB_RX_IQ_IMBALANCE, val32 & 0xFFFFFFFF)

    reg = result[candidate][7] & 0x3F
    val32 = t.read32(REG_OFDM0_XB_RX_IQ_IMBALANCE) & ~0xFC00
    val32 |= (reg << 10) & 0xFC00
    t.write32(REG_OFDM0_XB_RX_IQ_IMBALANCE, val32 & 0xFFFFFFFF)

    reg = (result[candidate][7] >> 6) & 0xF
    val32 = t.read32(REG_OFDM0_AGC_RSSI_TABLE) & ~0x0000F000
    val32 |= (reg << 12) & 0x0000F000
    t.write32(REG_OFDM0_AGC_RSSI_TABLE, val32 & 0xFFFFFFFF)


def simularity_compare(result, c1: int, c2: int, tx_paths: int = 1) -> bool:
    """Port of `rtl8xxxu_gen2_simularity_compare` (core.c:2930) — the 8192eu wrapper
    (``8192e.c:1256``) uses the gen2 variant, which sign-extends the odd (after-power) columns
    before the difference. Pure computation on ``result``: picks/merges the converged candidate
    into ``result[3]``. No device I/O. ``tx_paths > 1`` (2 for the 8192EU) widens the compared
    span to both paths."""
    bound = 8 if tx_paths > 1 else 4
    candidate = [-1, -1]
    simubitmap = 0

    for i in range(bound):
        if i & 1:
            tmp1 = result[c1][i]
            if tmp1 & 0x200:
                tmp1 |= 0xFFFFFC00
            tmp2 = result[c2][i]
            if tmp2 & 0x200:
                tmp2 |= 0xFFFFFC00
        else:
            tmp1 = result[c1][i]
            tmp2 = result[c2][i]

        diff = tmp1 - tmp2 if tmp1 > tmp2 else tmp2 - tmp1
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

    if not (simubitmap & 0x03):
        # Path A TX OK.
        for i in range(2):
            result[3][i] = result[c1][i]
    if not (simubitmap & 0x0C):
        # Path A RX OK.
        for i in range(2, 4):
            result[3][i] = result[c1][i]
    if not (simubitmap & 0x30) and tx_paths > 1:
        # Path B TX OK.
        for i in range(4, 6):
            result[3][i] = result[c1][i]
    if not (simubitmap & 0xC0) and tx_paths > 1:
        # Path B RX OK.
        for i in range(6, 8):
            result[3][i] = result[c1][i]
    return False


def phy_iq_calibrate(t: RTL8192EUTransport) -> None:
    """Port of `rtl8192eu_phy_iq_calibrate` (8192e.c:1236) — run up to 3 IQK iterations,
    pick the converged candidate via the gen2 similarity compare, apply both paths' matrices,
    then snapshot the OFDM IQ-correction registers into the recovery backup."""
    result = [[0] * 8 for _ in range(4)]
    candidate = -1
    path_a_ok = False
    path_b_ok = False
    backup = IqkBackup()

    for i in range(3):
        phy_iqcalibrate(t, result, i, backup)

        if i == 1 and simularity_compare(result, 0, 1, TX_PATHS):
            candidate = 0
            break

        if i == 2:
            if simularity_compare(result, 0, 2, TX_PATHS):
                candidate = 0
                break
            candidate = 1 if simularity_compare(result, 1, 2, TX_PATHS) else 3

    if candidate >= 0:
        reg_e94 = result[candidate][0]
        reg_ea4 = result[candidate][2]
        reg_ec4 = result[candidate][6]
        path_a_ok = True
        path_b_ok = True
    else:
        reg_e94 = 0x100
        reg_ea4 = 0
        reg_ec4 = 0

    if reg_e94 and candidate >= 0:
        fill_iqk_matrix_a(t, path_a_ok, result, candidate, reg_ea4 == 0)

    # 2T2R (rf_paths = 2, 8192e.c:490): always apply the path-B matrix; a failed path B
    # (candidate < 0) makes fill_iqk_matrix_b a no-op through ``path_b_ok``.
    fill_iqk_matrix_b(t, path_b_ok, result, candidate, reg_ec4 == 0)

    bb_recovery = [0] * len(IQK_PHY_IQ_BB_REG)
    save_regs(t, IQK_PHY_IQ_BB_REG, bb_recovery)


def phy_lc_calibrate(t: RTL8192EUTransport) -> None:
    """Port of `rtl8723a_phy_lc_calibrate` (core.c:3498) — LC (VCO) calibration, both paths.

    Guard against in-flight TX (``REG_OFDM1_LSTF``), park both RF paths in standby, pulse RF
    ``MODE_AG`` bit 15 to start the LC-tank cal, wait 100 ms, then restore. ``has_s0s1 == 0``
    for the 8192eu (8192e.c:1751), so the ``RF6052_REG_S0S1`` writes are skipped. ``tx_paths``
    is 2, so the path-B standby/restore branch runs (core.c:3519-3525, 3551-3553). Runs just
    before IQK."""
    lstf = t.read32(REG_OFDM1_LSTF)
    rf_amode = 0
    rf_bmode = 0
    if lstf & OFDM_LSTF_MASK:
        # Disable all continuous TX, park both paths in standby.
        t.write32(REG_OFDM1_LSTF, lstf & ~OFDM_LSTF_MASK & 0xFFFFFFFF)
        rf_amode = read_rfreg(t, RF_A, RF6052_REG_AC)
        write_rfreg(t, RF_A, RF6052_REG_AC, (rf_amode & 0x8FFFF) | 0x10000)
        rf_bmode = read_rfreg(t, RF_B, RF6052_REG_AC)
        write_rfreg(t, RF_B, RF6052_REG_AC, (rf_bmode & 0x8FFFF) | 0x10000)
    else:
        # Packet-TX case: block all queues.
        t.write8(REG_TXPAUSE, 0xFF)

    # Start LC calibration (RF MODE_AG bit 15), path A (has_s0s1 = 0 -> no S0S1 write).
    val32 = read_rfreg(t, RF_A, RF6052_REG_MODE_AG) | 0x08000
    write_rfreg(t, RF_A, RF6052_REG_MODE_AG, val32)
    time.sleep(0.100)

    # Restore original parameters.
    if lstf & OFDM_LSTF_MASK:
        t.write32(REG_OFDM1_LSTF, lstf)
        write_rfreg(t, RF_A, RF6052_REG_AC, rf_amode)
        write_rfreg(t, RF_B, RF6052_REG_AC, rf_bmode)
    else:
        t.write8(REG_TXPAUSE, 0x00)
