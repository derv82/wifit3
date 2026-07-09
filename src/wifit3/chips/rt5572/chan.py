"""rt2800usb channel tune (2.4 GHz + 5 GHz).

Mirrors the kernel's `rt2800_config_channel` dispatcher (rt2800lib.c:4161)
for the chips we support. The dispatcher branches by RF chip, NOT by
silicon — the same silicon ID can pair with different RF chips on
some platforms — but for the dongles wifit3 claims:

  silicon 0x5392 (RT5372 / Panda PAU05) → RF53xx code path (2.4 GHz only)
  silicon 0x3572 (RT3572 / AWUS051NH v2) → RF3052 code path (2.4 + 5 GHz)
  silicon 0x5592 (RT5572 / Panda PAU09)  → RF55xx code path  (M-B TBD)

M-A1 brought up RT3572 2.4 GHz; M-A2 (this file) extends `_set_channel_3572`
to the 5 GHz branch of `rt2800_config_channel_rf3052`.

[SRC] rt2800lib.c:2547-2795 (config_channel_rf3xxx + config_channel_rf3052)
      rt2800lib.c:3387-3483 (config_channel_rf53xx)
      rt2800lib.c:4161-4563 (config_channel dispatcher + post-RF block)
      rt2800lib.c:11435-11494 (rf_vals_3x table — 2.4 GHz + 5 GHz)
      rt2800lib.c:2447-2480 (freq_cal_mode1)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .bbp import (
    bbp_glrt_write,
    bbp_read,
    bbp_write,
    bbp_write_with_rx_chain,
)
from .constants import (
    BBP1_TX_ANTENNA,
    BBP3_HT40_MINUS,
    BBP3_RX_ANTENNA,
    BBP4_BANDWIDTH,
    CH_BUSY_STA,
    CH_BUSY_STA_SEC,
    CH_IDLE_STA,
    GPIO_CTRL,
    GPIO_CTRL_DIR7,
    GPIO_CTRL_VAL7,
    LDO_CFG0,
    LDO_CFG0_LDO_CORE_VLEVEL,
    MAC_DEBUG_INDEX,
    MAC_DEBUG_INDEX_XTAL,
    POWER_BOUND,
    POWER_BOUND_5G,
    RFCSR1_PLL_PD,
    RFCSR1_RF_BLOCK_EN,
    RFCSR1_RX0_PD,
    RFCSR1_RX1_PD,
    RFCSR1_RX2_PD,
    RFCSR1_TX0_PD,
    RFCSR1_TX1_PD,
    RFCSR1_TX2_PD,
    RFCSR3_VCOCAL_EN,
    RFCSR5_R1,
    RFCSR6_R1,
    RFCSR6_TXDIV,
    RFCSR7_BIT2,
    RFCSR7_BIT3,
    RFCSR7_BIT4,
    RFCSR7_BITS67,
    RFCSR7_RF_TUNING,
    RFCSR9_K,
    RFCSR9_MOD,
    RFCSR9_N,
    RFCSR11_MOD,
    RFCSR11_R,
    RFCSR12_DR0,
    RFCSR12_TX_POWER,
    RFCSR13_DR0,
    RFCSR13_TX_POWER,
    RFCSR16_TXMIXER_GAIN,
    RFCSR23_FREQ_OFFSET,
    RFCSR30_RX_H20M,
    RFCSR30_TX_H20M,
    RFCSR49_TX,
    RFCSR50_TX,
    RT_RT3572,
    RT_RT5392,
    RT_RT5592,
    TX_BAND_CFG_A,
    TX_BAND_CFG_BG_BIT,
    TX_BAND_CFG_HT40_MINUS,
    TX_BAND_CFG_REG,
    TX_PWR_CFG_0,
    TX_PWR_CFG_1,
    TX_PWR_CFG_2,
    TX_PWR_CFG_3,
    TX_PWR_CFG_4,
    TX_PIN_CFG_LNA_PE_A0_EN_BIT,
    TX_PIN_CFG_LNA_PE_A1_EN,
    TX_PIN_CFG_LNA_PE_G0_EN_BIT,
    TX_PIN_CFG_LNA_PE_G1_EN,
    TX_PIN_CFG_PA_PE_A0_EN_BIT,
    TX_PIN_CFG_PA_PE_A1_EN,
    TX_PIN_CFG_PA_PE_G0_EN_BIT,
    TX_PIN_CFG_PA_PE_G1_EN,
    TX_PIN_CFG_REG,
    TX_PIN_CFG_RFTR_EN_BIT,
    TX_PIN_CFG_TRSW_EN_BIT,
)
from .eeprom import (
    EEPROM_EIRP_MAX_TX_POWER_2GHZ,
    EEPROM_EIRP_MAX_TX_POWER_5GHZ,
    EEPROM_OFFSET_EIRP_MAX_TX_POWER,
    EEPROM_OFFSET_TXPOWER_A1,
    EEPROM_OFFSET_TXPOWER_A2,
    EEPROM_OFFSET_TXPOWER_BG1,
    EEPROM_OFFSET_TXPOWER_BG2,
    EEPROM_OFFSET_TXPOWER_BYRATE,
    EEPROM_TXPOWER_BYRATE_SIZE,
    EepromValues,
    IqCalChannel,
    IqCalibration,
    txpower_to_dev,
)
from .rfcsr import RfFilterCal, freq_cal_mode1_usb, rfcsr_read, rfcsr_write
from .transport import RT5572Transport

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# rf_vals_3x table — 2.4 GHz (channels 1-14) + 5 GHz (UNII-1/2 + HyperLAN-2
# + UNII-3, channels 36-173). Ported verbatim from rt2800lib.c:11435-11494.
# Tuple format: (rf1, rf2, rf3). For RF53xx (2.4-GHz silicon) these go to
# RFCSR8/11_R/9; for RF3052 they go to RFCSR2/6_R1/3 — see _set_channel_*.
# The half-channels (38, 46, 54, 62, 102, 110, ...) are kernel-table
# artefacts (5-MHz offsets used during channel-bonding); we list them so
# the table matches the kernel byte-for-byte but expose only the 20-MHz
# IEEE channels via CHANNELS_5G_NON_DFS / CHANNELS_5G_DFS below.
# ----------------------------------------------------------------------
_RF_VALS_3X = {
    # ---- 2.4 GHz ----
    1:  (241, 2, 2),
    2:  (241, 2, 7),
    3:  (242, 2, 2),
    4:  (242, 2, 7),
    5:  (243, 2, 2),
    6:  (243, 2, 7),
    7:  (244, 2, 2),
    8:  (244, 2, 7),
    9:  (245, 2, 2),
    10: (245, 2, 7),
    11: (246, 2, 2),
    12: (246, 2, 7),
    13: (247, 2, 2),
    14: (248, 2, 4),
    # ---- 5 GHz UNII-1 + half-channel offsets ----
    36: (0x56, 0,  4),
    38: (0x56, 0,  6),
    40: (0x56, 0,  8),
    44: (0x57, 0,  0),
    46: (0x57, 0,  2),
    48: (0x57, 0,  4),
    52: (0x57, 0,  8),
    54: (0x57, 0, 10),
    56: (0x58, 0,  0),
    60: (0x58, 0,  4),
    62: (0x58, 0,  6),
    64: (0x58, 0,  8),
    # ---- 5 GHz HyperLAN-2 ----
    100: (0x5b, 0,  8),
    102: (0x5b, 0, 10),
    104: (0x5c, 0,  0),
    108: (0x5c, 0,  4),
    110: (0x5c, 0,  6),
    112: (0x5c, 0,  8),
    116: (0x5d, 0,  0),
    118: (0x5d, 0,  2),
    120: (0x5d, 0,  4),
    124: (0x5d, 0,  8),
    126: (0x5d, 0, 10),
    128: (0x5e, 0,  0),
    132: (0x5e, 0,  4),
    134: (0x5e, 0,  6),
    136: (0x5e, 0,  8),
    140: (0x5f, 0,  0),
    # ---- 5 GHz UNII-3 + extended UNII-3 ----
    149: (0x5f, 0,  9),
    151: (0x5f, 0, 11),
    153: (0x60, 0,  1),
    157: (0x60, 0,  5),
    159: (0x60, 0,  7),
    161: (0x60, 0,  9),
    165: (0x61, 0,  1),
    167: (0x61, 0,  3),
    169: (0x61, 0,  5),
    171: (0x61, 0,  7),
    173: (0x61, 0,  9),
}

# Standard 20-MHz IEEE channels — exposed to the scanner/hopper.
# DFS channels are split out so callers (driver.SUPPORTED_CHANNELS) can
# pick whether to advertise them. wifit3 currently ships non-DFS only;
# the silicon will happily tune the DFS list if asked (it's RX-only).
CHANNELS_5G_NON_DFS = (36, 40, 44, 48, 149, 153, 157, 161, 165)
CHANNELS_5G_DFS = (
    52, 56, 60, 64,
    100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140,
)
CHANNELS_5G_ALL = CHANNELS_5G_NON_DFS + CHANNELS_5G_DFS

# ----------------------------------------------------------------------
# RF53xx 2.4 GHz channel tune (RT5390/RT5392).
# Body unchanged from M-A1; uses the shared `_RF_VALS_3X` table.
# RF53xx silicon is 2.4 GHz only — caller must not pass channels > 14.
# ----------------------------------------------------------------------
def _set_channel_5392(
    t: RT5572Transport,
    channel: int,
    *,
    freq_offset: int = 0,
    lna_gain: int = 0,
) -> None:
    rf1, rf2, rf3 = _RF_VALS_3X[channel]

    rfcsr_write(t, 8, rf1)
    rfcsr_write(t, 9, rf3)
    rfcsr = rfcsr_read(t, 11)
    rfcsr = (rfcsr & ~RFCSR11_R) | (rf2 & RFCSR11_R)
    rfcsr_write(t, 11, rfcsr & 0xFF)

    rfcsr = rfcsr_read(t, 1)
    rfcsr |= RFCSR1_RX1_PD | RFCSR1_TX1_PD
    rfcsr |= RFCSR1_RF_BLOCK_EN
    rfcsr |= RFCSR1_PLL_PD
    rfcsr |= RFCSR1_RX0_PD
    rfcsr |= RFCSR1_TX0_PD
    rfcsr_write(t, 1, rfcsr & 0xFF)

    freq_cal_mode1_usb(t, freq_offset=freq_offset)

    rfcsr = rfcsr_read(t, 30)
    rfcsr &= ~(RFCSR30_TX_H20M | RFCSR30_RX_H20M) & 0xFF
    rfcsr_write(t, 30, rfcsr)

    rfcsr = rfcsr_read(t, 3)
    rfcsr |= RFCSR3_VCOCAL_EN
    rfcsr_write(t, 3, rfcsr & 0xFF)

    nf = (0x37 - (lna_gain & 0xFF)) & 0xFF
    bbp_write(t, 62, nf)
    bbp_write(t, 63, nf)
    bbp_write(t, 64, nf)
    bbp_write(t, 86, 0x00)

    t.write32(TX_BAND_CFG_REG, TX_BAND_CFG_BG_BIT)

    tx_pin = (
        TX_PIN_CFG_PA_PE_G0_EN_BIT
        | TX_PIN_CFG_LNA_PE_A0_EN_BIT
        | TX_PIN_CFG_LNA_PE_G0_EN_BIT
        | TX_PIN_CFG_RFTR_EN_BIT
        | TX_PIN_CFG_TRSW_EN_BIT
    )
    t.write32(TX_PIN_CFG_REG, tx_pin)

    time.sleep(0.001)


# ----------------------------------------------------------------------
# RT3572 / RF3052 2.4 GHz + 5 GHz channel tune.
#
# Single function with a band-switch (`is_2g = channel <= 14`) — matches
# the kernel's `rt2800_config_channel_rf3052` shape (one function,
# `if (rf->channel <= 14)` branches throughout). The post-RF tail is
# `rt2800_config_channel`'s body downstream of the RF-chip dispatch.
#
# [SRC] rt2800lib.c:2625-2795 (config_channel_rf3052)
#       rt2800lib.c:4257-4423 (post-RF tail of config_channel — RT3572
#                              branches at 4298+, 4308+, 4329+, 4413+)
#       rt2800lib.c:11435-11494 (rf_vals_3x channels 1..173)
# ----------------------------------------------------------------------
def _set_channel_3572(
    t: RT5572Transport,
    channel: int,
    *,
    freq_offset: int = 0,
    lna_gain: int = 0,
    txmixer_gain_24g: int = 0,
    txmixer_gain_5g: int = 0,
    tx_chain_num: int = 2,
    rx_chain_num: int = 2,
    has_cap_bt_coexist: bool = False,
    has_cap_external_lna_a: bool = False,
    cal_result: Optional[RfFilterCal] = None,
    default_power1: int = 0,
    default_power2: int = 0,
) -> None:
    if cal_result is None:
        raise ValueError(
            "_set_channel_3572 requires cal_result from init_rfcsr_3572"
        )

    rf1, rf2, rf3 = _RF_VALS_3X[channel]
    is_2g = channel <= 14

    # ---- (1) BBP25/26 — restore-from-cal for 2.4G, hardcoded for 5G ----
    # 5G hardcodes 0x09/0xFF for IQ phase correction enable + value.
    # [SRC] rt2800lib.c:2634-2640
    if is_2g:
        bbp_write(t, 25, cal_result.bbp25)
        bbp_write(t, 26, cal_result.bbp26)
    else:
        bbp_write(t, 25, 0x09)
        bbp_write(t, 26, 0xFF)

    # ---- (2) Synthesizer + dividers (RF3052 layout) -----------------
    rfcsr_write(t, 2, rf1)
    rfcsr_write(t, 3, rf3)

    rfcsr = rfcsr_read(t, 6)
    rfcsr = (rfcsr & ~RFCSR6_R1) | (rf2 & RFCSR6_R1)
    # TXDIV: 2.4G → 2, 5G → 1.  [SRC] rt2800lib.c:2647-2650
    txdiv = 2 if is_2g else 1
    rfcsr = (rfcsr & ~RFCSR6_TXDIV) | ((txdiv << 2) & RFCSR6_TXDIV)
    rfcsr_write(t, 6, rfcsr & 0xFF)

    rfcsr = rfcsr_read(t, 5)
    # R1: 2.4G → 1, 5G → 2.  [SRC] rt2800lib.c:2653-2657
    r1 = 1 if is_2g else 2
    rfcsr = (rfcsr & ~RFCSR5_R1) | ((r1 << 2) & RFCSR5_R1)
    rfcsr_write(t, 5, rfcsr & 0xFF)

    # ---- (3) TX power (RFCSR12/13) ----------------------------------
    # 2.4G: DR0=3, TX_POWER = default_power1 (low 5 bits).
    # 5G:   DR0=7, TX_POWER = (p & 0x3) | ((p & 0xC) << 1) — 4-bit
    #       split encoding. [SRC] rt2800lib.c:2660-2683
    def _tx_power_5g(p: int) -> int:
        return ((p & 0x3) | ((p & 0xC) << 1)) & 0xFF

    rfcsr = rfcsr_read(t, 12)
    if is_2g:
        rfcsr = (rfcsr & ~RFCSR12_DR0) | ((3 << 5) & RFCSR12_DR0)
        rfcsr = (rfcsr & ~RFCSR12_TX_POWER) | (default_power1 & RFCSR12_TX_POWER)
    else:
        rfcsr = (rfcsr & ~RFCSR12_DR0) | ((7 << 5) & RFCSR12_DR0)
        rfcsr = (
            (rfcsr & ~RFCSR12_TX_POWER)
            | (_tx_power_5g(default_power1) & RFCSR12_TX_POWER)
        )
    rfcsr_write(t, 12, rfcsr & 0xFF)

    rfcsr = rfcsr_read(t, 13)
    if is_2g:
        rfcsr = (rfcsr & ~RFCSR13_DR0) | ((3 << 5) & RFCSR13_DR0)
        rfcsr = (rfcsr & ~RFCSR13_TX_POWER) | (default_power2 & RFCSR13_TX_POWER)
    else:
        rfcsr = (rfcsr & ~RFCSR13_DR0) | ((7 << 5) & RFCSR13_DR0)
        rfcsr = (
            (rfcsr & ~RFCSR13_TX_POWER)
            | (_tx_power_5g(default_power2) & RFCSR13_TX_POWER)
        )
    rfcsr_write(t, 13, rfcsr & 0xFF)

    # ---- (4) RFCSR1 chain power-downs -------------------------------
    rfcsr = rfcsr_read(t, 1)
    pd_mask = (
        RFCSR1_RX0_PD | RFCSR1_TX0_PD
        | RFCSR1_RX1_PD | RFCSR1_TX1_PD
        | RFCSR1_RX2_PD | RFCSR1_TX2_PD
    )
    rfcsr &= ~pd_mask & 0xFF

    if has_cap_bt_coexist:
        # [SRC] rt2800lib.c:2693-2699
        # BT coex on 2.4 GHz powers down the primary chains so BT shares
        # the antenna. On 5 GHz only the tertiary chains are powered
        # down (BT is 2.4 GHz only so no antenna sharing).
        if is_2g:
            rfcsr |= RFCSR1_RX0_PD | RFCSR1_TX0_PD
        rfcsr |= RFCSR1_RX2_PD | RFCSR1_TX2_PD
    else:
        if tx_chain_num == 1:
            rfcsr |= RFCSR1_TX1_PD | RFCSR1_TX2_PD
        elif tx_chain_num == 2:
            rfcsr |= RFCSR1_TX2_PD
        if rx_chain_num == 1:
            rfcsr |= RFCSR1_RX1_PD | RFCSR1_RX2_PD
        elif rx_chain_num == 2:
            rfcsr |= RFCSR1_RX2_PD
    rfcsr_write(t, 1, rfcsr & 0xFF)

    # ---- (5) Frequency offset trim ---------------------------------
    rfcsr = rfcsr_read(t, 23)
    rfcsr = (rfcsr & ~RFCSR23_FREQ_OFFSET) | (freq_offset & RFCSR23_FREQ_OFFSET)
    rfcsr_write(t, 23, rfcsr & 0xFF)

    # ---- (6) Replay filter-calibration values ----------------------
    # HT20 monitor only — always use calibration_bw20 (both bands).
    rfcsr_write(t, 24, cal_result.calibration_bw20)
    rfcsr_write(t, 31, cal_result.calibration_bw20)

    # ---- (7) RF3052 band-specific block ----------------------------
    if is_2g:
        # [SRC] rt2800lib.c:2733-2749
        rfcsr_write(t, 7, 0xD8)
        rfcsr_write(t, 9, 0xC3)
        rfcsr_write(t, 10, 0xF1)
        rfcsr_write(t, 11, 0xB9)
        rfcsr_write(t, 15, 0x53)

        rfcsr16 = 0x4C
        rfcsr16 = (rfcsr16 & ~RFCSR16_TXMIXER_GAIN) | (
            txmixer_gain_24g & RFCSR16_TXMIXER_GAIN
        )
        rfcsr_write(t, 16, rfcsr16 & 0xFF)

        rfcsr_write(t, 17, 0x23)
        rfcsr_write(t, 19, 0x93)
        rfcsr_write(t, 20, 0xB3)
        rfcsr_write(t, 25, 0x15)
        rfcsr_write(t, 26, 0x85)
        rfcsr_write(t, 27, 0x00)
        rfcsr_write(t, 29, 0x9B)
    else:
        # [SRC] rt2800lib.c:2750-2782 — 5 GHz block.
        # RFCSR7 is RMW (preserves RF_TUNING + bits we don't touch);
        # set BIT2=1, BIT4=1, clear BIT3 + BITS67.
        rfcsr = rfcsr_read(t, 7)
        rfcsr = (rfcsr & ~(RFCSR7_BIT3 | RFCSR7_BITS67)) | (
            RFCSR7_BIT2 | RFCSR7_BIT4
        )
        rfcsr_write(t, 7, rfcsr & 0xFF)

        rfcsr_write(t, 9, 0xC0)
        rfcsr_write(t, 10, 0xF1)
        rfcsr_write(t, 11, 0x00)
        rfcsr_write(t, 15, 0x43)

        rfcsr16 = 0x7A
        rfcsr16 = (rfcsr16 & ~RFCSR16_TXMIXER_GAIN) | (
            txmixer_gain_5g & RFCSR16_TXMIXER_GAIN
        )
        rfcsr_write(t, 16, rfcsr16 & 0xFF)

        rfcsr_write(t, 17, 0x23)

        # Per sub-band RFCSR19/20/25.  [SRC] rt2800lib.c:2766-2778
        if channel <= 64:
            rfcsr_write(t, 19, 0xB7)
            rfcsr_write(t, 20, 0xF6)
            rfcsr_write(t, 25, 0x3D)
        elif channel <= 128:
            rfcsr_write(t, 19, 0x74)
            rfcsr_write(t, 20, 0xF4)
            rfcsr_write(t, 25, 0x01)
        else:
            rfcsr_write(t, 19, 0x72)
            rfcsr_write(t, 20, 0xF3)
            rfcsr_write(t, 25, 0x01)

        rfcsr_write(t, 26, 0x87)
        rfcsr_write(t, 27, 0x01)
        rfcsr_write(t, 29, 0x9F)

    # ---- (8) GPIO_CTRL band-switch (#7 high = 2.4G, low = 5G) -------
    reg = t.read32(GPIO_CTRL)
    reg &= ~GPIO_CTRL_DIR7              # output
    if is_2g:
        reg |= GPIO_CTRL_VAL7
    else:
        reg &= ~GPIO_CTRL_VAL7
    t.write32(GPIO_CTRL, reg & 0xFFFFFFFF)

    # ---- (9) Kick the new tune (RFCSR7_RF_TUNING = 1) --------------
    rfcsr = rfcsr_read(t, 7)
    rfcsr |= RFCSR7_RF_TUNING
    rfcsr_write(t, 7, rfcsr & 0xFF)

    # ---- (10) Post-RF dispatcher tail (rt2800lib.c:4257+) ----------
    # NB: kernel's RFCSR30 H20M + RFCSR3 VCOCAL block at lines
    # 4228-4254 is gated on RF chip == RF3070/53xx → RT3572 (RF3052)
    # skips it entirely. We jump straight to the BBP changes.

    # BBP noise-floor writes (uses lna_gain from EEPROM).
    nf = (0x37 - (lna_gain & 0xFF)) & 0xFF
    bbp_write(t, 62, nf)
    bbp_write(t, 63, nf)
    bbp_write(t, 64, nf)
    bbp_write(t, 86, 0x00)                # RT3572 != RT6352 → 0

    # BBP82/75 — band-specific. [SRC] rt2800lib.c:4308-4345
    if is_2g:
        # 2.4G else-branch: not RT5390/RT5392/RT6352 so this runs.
        # has_cap_external_lna_bg requires EEPROM; without it, else
        # branch: BBP82 = 0x84 (RT3572 is not RT3593), BBP75 = 0x50.
        bbp_write(t, 82, 0x84)
        bbp_write(t, 75, 0x50)
    else:
        # 5G branch: RT3572 → BBP82 = 0x94.
        bbp_write(t, 82, 0x94)
        # BBP75: 0x46 if external LNA-A, else 0x50.
        bbp_write(t, 75, 0x46 if has_cap_external_lna_a else 0x50)

    # TX_BAND_CFG — HT20 + band routing.
    reg = t.read32(TX_BAND_CFG_REG)
    reg &= ~TX_BAND_CFG_HT40_MINUS
    if is_2g:
        reg &= ~TX_BAND_CFG_A
        reg |= TX_BAND_CFG_BG_BIT
    else:
        reg |= TX_BAND_CFG_A
        reg &= ~TX_BAND_CFG_BG_BIT
    t.write32(TX_BAND_CFG_REG, reg & 0xFFFFFFFF)

    # RT3572-only RFCSR8 pre-AGC write (both bands).
    rfcsr_write(t, 8, 0)

    # Assemble TX_PIN_CFG. Kernel starts from 0 (not RT6352).
    # [SRC] rt2800lib.c:4356-4411
    tx_pin = 0

    # PA enables (per tx_chain_num switch, with case fall-through).
    # 5 GHz uses A0/A1; 2.4 GHz uses G0/G1.
    if tx_chain_num >= 3:
        raise NotImplementedError(
            "tx_chain_num >= 3 not supported on RT3572 — 2T2R is the "
            "documented hw config for our supported dongles"
        )
    if tx_chain_num >= 2:
        if is_2g:
            tx_pin |= TX_PIN_CFG_PA_PE_G1_EN
        else:
            tx_pin |= TX_PIN_CFG_PA_PE_A1_EN
    # Primary PAs (always — case 1).
    # BT-coex on 2.4 GHz forces PA_PE_G0_EN regardless of channel.
    if has_cap_bt_coexist:
        tx_pin |= TX_PIN_CFG_PA_PE_G0_EN_BIT
    else:
        if is_2g:
            tx_pin |= TX_PIN_CFG_PA_PE_G0_EN_BIT
        else:
            tx_pin |= TX_PIN_CFG_PA_PE_A0_EN_BIT

    # LNA enables: kernel sets BOTH the A-side and G-side enables for
    # every active RX chain, regardless of band. [SRC] rt2800lib.c:4390-4405
    if rx_chain_num >= 3:
        raise NotImplementedError(
            "rx_chain_num >= 3 not supported on RT3572"
        )
    if rx_chain_num >= 2:
        tx_pin |= TX_PIN_CFG_LNA_PE_A1_EN | TX_PIN_CFG_LNA_PE_G1_EN
    tx_pin |= TX_PIN_CFG_LNA_PE_A0_EN_BIT | TX_PIN_CFG_LNA_PE_G0_EN_BIT

    # RFTR + TRSW always on.
    tx_pin |= TX_PIN_CFG_RFTR_EN_BIT | TX_PIN_CFG_TRSW_EN_BIT

    t.write32(TX_PIN_CFG_REG, tx_pin & 0xFFFFFFFF)

    # RT3572 AGC init: RFCSR8 = 0x80, then BBP66 across each RX chain.
    # Formula differs by band — [SRC] rt2800lib.c:4413-4423.
    rfcsr_write(t, 8, 0x80)
    if is_2g:
        bbp66 = (0x1C + 2 * (lna_gain & 0xFF)) & 0xFF
    else:
        bbp66 = (0x22 + ((lna_gain & 0xFF) * 5) // 3) & 0xFF
    bbp_write_with_rx_chain(t, 66, bbp66, rx_chain_num=rx_chain_num)

    # RT3572 settle delay — kernel rt2800lib.c:4465
    time.sleep(0.0015)

    # BBP4 BANDWIDTH = 0 (HT20).
    bbp = bbp_read(t, 4)
    bbp = bbp & ~BBP4_BANDWIDTH
    bbp_write(t, 4, bbp & 0xFF)

    # BBP3 HT40_MINUS = 0 (HT20).
    bbp = bbp_read(t, 3)
    bbp = bbp & ~BBP3_HT40_MINUS
    bbp_write(t, 3, bbp & 0xFF)

    time.sleep(0.001)

    # ---- (11) Per-rate TX power (kernel rt2800_config_txpower_rt28xx) ----
    # The PHY reads TX_PWR_CFG_0..4 to pick TX power per modulation rate; with
    # placeholder values the TX engine still reports TX_SUCCESS but emits
    # near-zero RF. This card's EFUSE TXPOWER region is erased (0xFF), so there's
    # nothing to derive from — write the exact values the in-tree rt2800usb
    # driver programs for it, landing the radio in the airmon-ng state.
    # [WIRE] captures_rt3572_tx_diff/aireplay.pcap (ch1). Note: 0x0A0A0A0A would
    # zero power on half the rates (its high nibbles are 0), incl. the 1 Mbps CCK
    # that mgmt/auth frames ride — so it must not be used as the default.
    # TODO: derive per-channel from EEPROM TXPOWER_BYRATE once a burned card exists.
    _RT3572_TX_PWR_CFG_DEFAULTS = (
        (TX_PWR_CFG_0, 0xCCCCAAAA),
        (TX_PWR_CFG_1, 0xCCCCAACC),
        (TX_PWR_CFG_2, 0xCCCCAACC),
        (TX_PWR_CFG_3, 0xCCCCAACC),
        (TX_PWR_CFG_4, 0xCCCCAACC),
    )
    for reg, val in _RT3572_TX_PWR_CFG_DEFAULTS:
        t.write32(reg, val)

    # BBP1.TX_POWER_CTRL = 0 — global TX power offset of 0 dBm (no
    # -6/-12/+6 adjustment). Kernel rt2800_config_txpower_rt28xx sets this
    # based on delta from EEPROM; we just zero it for unburned EEPROM.
    bbp1 = bbp_read(t, 1)
    bbp1 &= ~0x03   # BBP1_TX_POWER_CTRL = bits[1:0]
    bbp_write(t, 1, bbp1 & 0xFF)

    # Clear channel-activity counters as a side-effect of reading.
    t.read32(CH_IDLE_STA)
    t.read32(CH_BUSY_STA)
    t.read32(CH_BUSY_STA_SEC)

    logger.debug(
        "set_channel_3572: ch=%d band=%s rf=(%d,%d,%d) tx/rx_chain=(%d,%d) "
        "cal_bw20=0x%02x bbp25/26=0x%02x/0x%02x freq_off=%d "
        "txmixer=(%d,%d) bbp66=0x%02x",
        channel, "2g" if is_2g else "5g", rf1, rf2, rf3,
        tx_chain_num, rx_chain_num,
        cal_result.calibration_bw20, cal_result.bbp25, cal_result.bbp26,
        freq_offset, txmixer_gain_24g, txmixer_gain_5g, bbp66,
    )


# ----------------------------------------------------------------------
# RT5592 / RF5592 channel tables — 2.4 GHz only for M-B1; 5 GHz rows
# land in M-B2 along with the 5 GHz config-channel branch.
#
# Channel struct is 5-field (channel, N, K, mod, R) — encoded into
# RFCSR8/9/11 via RFCSR9_K/_N/_MOD + RFCSR11_R/_MOD. The N/K/mod/R
# values differ between xtal20 and xtal40 — picked at runtime from
# MAC_DEBUG_INDEX.XTAL.
#
# [SRC] rt2800lib.c:11578-11710 (rf_vals_5592_xtal20 / xtal40)
# ----------------------------------------------------------------------
_RF_VALS_5592_XTAL20 = {
    # channel: (N, K, mod, R) — [SRC] rt2800lib.c:11578-11642
    1:  (482, 4, 10, 3),
    2:  (483, 4, 10, 3),
    3:  (484, 4, 10, 3),
    4:  (485, 4, 10, 3),
    5:  (486, 4, 10, 3),
    6:  (487, 4, 10, 3),
    7:  (488, 4, 10, 3),
    8:  (489, 4, 10, 3),
    9:  (490, 4, 10, 3),
    10: (491, 4, 10, 3),
    11: (492, 4, 10, 3),
    12: (493, 4, 10, 3),
    13: (494, 4, 10, 3),
    14: (496, 8, 10, 3),
    # 5 GHz UNII-1/2 (ch 36-64).
    36: (172, 8, 12, 1),
    38: (173, 0, 12, 1),
    40: (173, 4, 12, 1),
    42: (173, 8, 12, 1),
    44: (174, 0, 12, 1),
    46: (174, 4, 12, 1),
    48: (174, 8, 12, 1),
    50: (175, 0, 12, 1),
    52: (175, 4, 12, 1),
    54: (175, 8, 12, 1),
    56: (176, 0, 12, 1),
    58: (176, 4, 12, 1),
    60: (176, 8, 12, 1),
    62: (177, 0, 12, 1),
    64: (177, 4, 12, 1),
    # 5 GHz UNII-2-ext (ch 100-140).
    100: (183, 4, 12, 1),
    102: (183, 8, 12, 1),
    104: (184, 0, 12, 1),
    106: (184, 4, 12, 1),
    108: (184, 8, 12, 1),
    110: (185, 0, 12, 1),
    112: (185, 4, 12, 1),
    114: (185, 8, 12, 1),
    116: (186, 0, 12, 1),
    118: (186, 4, 12, 1),
    120: (186, 8, 12, 1),
    122: (187, 0, 12, 1),
    124: (187, 4, 12, 1),
    126: (187, 8, 12, 1),
    128: (188, 0, 12, 1),
    130: (188, 4, 12, 1),
    132: (188, 8, 12, 1),
    134: (189, 0, 12, 1),
    136: (189, 4, 12, 1),
    138: (189, 8, 12, 1),
    140: (190, 0, 12, 1),
    # 5 GHz UNII-3 (ch 149-165).
    149: (191, 6, 12, 1),
    151: (191, 10, 12, 1),
    153: (192, 2, 12, 1),
    155: (192, 6, 12, 1),
    157: (192, 10, 12, 1),
    159: (193, 2, 12, 1),
    161: (193, 6, 12, 1),
    165: (194, 2, 12, 1),
}

_RF_VALS_5592_XTAL40 = {
    # channel: (N, K, mod, R) — [SRC] rt2800lib.c:11644-11707
    1:  (241, 2, 10, 3),
    2:  (241, 7, 10, 3),
    3:  (242, 2, 10, 3),
    4:  (242, 7, 10, 3),
    5:  (243, 2, 10, 3),
    6:  (243, 7, 10, 3),
    7:  (244, 2, 10, 3),
    8:  (244, 7, 10, 3),
    9:  (245, 2, 10, 3),
    10: (245, 7, 10, 3),
    11: (246, 2, 10, 3),
    12: (246, 7, 10, 3),
    13: (247, 2, 10, 3),
    14: (248, 4, 10, 3),
    # 5 GHz UNII-1/2 (ch 36-64).
    36: (86, 4, 12, 1),
    38: (86, 6, 12, 1),
    40: (86, 8, 12, 1),
    42: (86, 10, 12, 1),
    44: (87, 0, 12, 1),
    46: (87, 2, 12, 1),
    48: (87, 4, 12, 1),
    50: (87, 6, 12, 1),
    52: (87, 8, 12, 1),
    54: (87, 10, 12, 1),
    56: (88, 0, 12, 1),
    58: (88, 2, 12, 1),
    60: (88, 4, 12, 1),
    62: (88, 6, 12, 1),
    64: (88, 8, 12, 1),
    # 5 GHz UNII-2-ext (ch 100-140).
    100: (91, 8, 12, 1),
    102: (91, 10, 12, 1),
    104: (92, 0, 12, 1),
    106: (92, 2, 12, 1),
    108: (92, 4, 12, 1),
    110: (92, 6, 12, 1),
    112: (92, 8, 12, 1),
    114: (92, 10, 12, 1),
    116: (93, 0, 12, 1),
    118: (93, 2, 12, 1),
    120: (93, 4, 12, 1),
    122: (93, 6, 12, 1),
    124: (93, 8, 12, 1),
    126: (93, 10, 12, 1),
    128: (94, 0, 12, 1),
    130: (94, 2, 12, 1),
    132: (94, 4, 12, 1),
    134: (94, 6, 12, 1),
    136: (94, 8, 12, 1),
    138: (94, 10, 12, 1),
    140: (95, 0, 12, 1),
    # 5 GHz UNII-3 (ch 149-165).
    149: (95, 9, 12, 1),
    151: (95, 11, 12, 1),
    153: (96, 1, 12, 1),
    155: (96, 3, 12, 1),
    157: (96, 5, 12, 1),
    159: (96, 7, 12, 1),
    161: (96, 9, 12, 1),
    165: (97, 1, 12, 1),
}


def is_xtal_40mhz(t: RT5572Transport) -> bool:
    """Read MAC_DEBUG_INDEX.XTAL — 1 = 40 MHz crystal, 0 = 20 MHz.

    The PAU09 N600's actual xtal isn't documented; we read it at
    runtime and pick the matching RF channel table. Kernel does the
    same probe in rt2800_probe_hw_mode.  [SRC] rt2800lib.c:11844-11852
    """
    return bool(t.read32(MAC_DEBUG_INDEX) & MAC_DEBUG_INDEX_XTAL)


# ----------------------------------------------------------------------
# rt2800_iq_calibrate — RT5592-only per-tune IQ trim.
# Writes 8 BBP158/159 pairs (TX0/TX1 × gain/phase × per-band selection)
# + 2 global pairs (RF IQ compensation + imbalance). Called from
# _set_channel_5592_{2g,5g} after the channel-tune RF writes settle.
# [SRC] rt2800lib.c:4026-4110
# ----------------------------------------------------------------------
def iq_calibrate(t: RT5572Transport, channel: int, iq: IqCalChannel | None) -> None:
    """Apply per-channel IQ trim. If ``iq`` is None, falls back to the
    all-zero kernel default (matches kernel's `cal = 0` when channel is
    outside any known sub-band)."""
    if iq is None:
        iq = IqCalChannel(0, 0, 0, 0, 0, 0)

    # TX0 IQ Gain  ─ BBP158=0x2c, BBP159=cal.
    bbp_write(t, 158, 0x2C)
    bbp_write(t, 159, iq.tx0_gain & 0xFF)
    # TX0 IQ Phase ─ BBP158=0x2d, BBP159=cal.
    bbp_write(t, 158, 0x2D)
    bbp_write(t, 159, iq.tx0_phase & 0xFF)
    # TX1 IQ Gain  ─ BBP158=0x4a, BBP159=cal.
    bbp_write(t, 158, 0x4A)
    bbp_write(t, 159, iq.tx1_gain & 0xFF)
    # TX1 IQ Phase ─ BBP158=0x4b, BBP159=cal.
    bbp_write(t, 158, 0x4B)
    bbp_write(t, 159, iq.tx1_phase & 0xFF)

    # Global RF IQ compensation + imbalance. Kernel applies the
    # 0xFF→0 fallback per byte; EepromValues._eeprom_byte_or_zero
    # already did it, so we pass through verbatim.
    bbp_write(t, 158, 0x04)
    bbp_write(t, 159, iq.rf_iq_comp & 0xFF)
    bbp_write(t, 158, 0x03)
    bbp_write(t, 159, iq.rf_iq_imbal & 0xFF)


# ----------------------------------------------------------------------
# RT5572 / RF5592 2.4 GHz channel tune.
#
# Mirrors rt2800_config_channel_rf55xx (rt2800lib.c:3485-3758) for the
# `rf->channel <= 14` branch, then the post-RF tail of
# rt2800_config_channel (lines 4257-4555) for the bits that fire on
# RT5592 specifically. M-B2 will extend to the 5 GHz branch.
#
# Defaults match kernel constants at the top of config_channel_rf55xx:
#   is_11b = false       (we don't run pure CCK)
#   is_type_ep = false   (no high-power ext-PA chip variant)
#
# Deferred for M-B1 (folds into M-B2):
#   * rt2800_iq_calibrate — RT5592-only per-tune IQ trim from EFUSE.
#     Without it RX should still work, just degraded SNR. If 2.4G
#     proves silent on hw, we pull it forward.
# ----------------------------------------------------------------------
def _set_channel_5592_2g(
    t: RT5572Transport,
    channel: int,
    *,
    n: int,
    k: int,
    mod: int,
    r: int,
    freq_offset: int = 0,
    lna_gain: int = 0,
    tx_chain_num: int = 2,
    rx_chain_num: int = 2,
    has_cap_bt_coexist: bool = False,
    has_cap_external_lna_bg: bool = False,
    default_power1: int = 0,
    default_power2: int = 0,
    iq_cal: IqCalChannel | None = None,
) -> None:
    # ---- (1) LDO_CFG0 — VLEVEL=0 for HT20 2.4 GHz. [SRC] 3498-3501 ----
    reg = t.read32(LDO_CFG0)
    reg = (reg & ~LDO_CFG0_LDO_CORE_VLEVEL) | ((0 << 26) & LDO_CFG0_LDO_CORE_VLEVEL)
    t.write32(LDO_CFG0, reg & 0xFFFFFFFF)

    # ---- (2) Synthesizer: RFCSR8/9/11 packed from {N, K, mod, R} ----
    # [SRC] 3504-3515
    rfcsr_write(t, 8, n & 0xFF)

    rfcsr = rfcsr_read(t, 9)
    rfcsr = (rfcsr & ~RFCSR9_K) | (k & RFCSR9_K)
    rfcsr = (rfcsr & ~RFCSR9_N) | (((n & 0x100) >> 8 << 4) & RFCSR9_N)
    rfcsr = (rfcsr & ~RFCSR9_MOD) | ((((mod - 8) & 0x4) >> 2 << 7) & RFCSR9_MOD)
    rfcsr_write(t, 9, rfcsr & 0xFF)

    rfcsr = rfcsr_read(t, 11)
    rfcsr = (rfcsr & ~RFCSR11_R) | ((r - 1) & RFCSR11_R)
    rfcsr = (rfcsr & ~RFCSR11_MOD) | (((mod - 8) & 0x3) << 6 & RFCSR11_MOD)
    rfcsr_write(t, 11, rfcsr & 0xFF)

    # ---- (3) 2.4 GHz fixed-value RFCSR block. [SRC] 3517-3547 ----
    rfcsr_write(t, 10, 0x90)
    # Kernel comment: "FIXME: RF11 overwrite?" — kernel deliberately
    # clobbers the synthesizer R/MOD bits from step 2 with 0x4A here.
    # Ported as-is.
    rfcsr_write(t, 11, 0x4A)
    rfcsr_write(t, 12, 0x52)
    rfcsr_write(t, 13, 0x42)
    rfcsr_write(t, 22, 0x40)
    rfcsr_write(t, 24, 0x4A)
    rfcsr_write(t, 25, 0x80)
    rfcsr_write(t, 27, 0x42)
    rfcsr_write(t, 36, 0x80)
    rfcsr_write(t, 37, 0x08)
    rfcsr_write(t, 38, 0x89)
    rfcsr_write(t, 39, 0x1B)
    rfcsr_write(t, 40, 0x0D)
    rfcsr_write(t, 41, 0x9B)
    rfcsr_write(t, 42, 0xD5)
    rfcsr_write(t, 43, 0x72)
    rfcsr_write(t, 44, 0x0E)
    rfcsr_write(t, 45, 0xA2)
    rfcsr_write(t, 46, 0x6B)
    rfcsr_write(t, 48, 0x10)
    rfcsr_write(t, 51, 0x3E)
    rfcsr_write(t, 52, 0x48)
    rfcsr_write(t, 54, 0x38)
    rfcsr_write(t, 56, 0xA1)
    rfcsr_write(t, 57, 0x00)
    rfcsr_write(t, 58, 0x39)
    rfcsr_write(t, 60, 0x45)
    rfcsr_write(t, 61, 0x91)
    rfcsr_write(t, 62, 0x39)

    # ---- (4) Channel-edge tweaks. [SRC] 3551-3553 ----
    # RFCSR23/59 = 0x07 for ch 1-10, 0x06 for ch 11-14.
    rfcsr23_59 = 0x07 if channel <= 10 else 0x06
    rfcsr_write(t, 23, rfcsr23_59)
    rfcsr_write(t, 59, rfcsr23_59)

    # ---- (5) OFDM mode (is_11b=false, is_type_ep=false). [SRC] 3568 ----
    rfcsr_write(t, 55, 0x43)

    # ---- (6) TX power: RFCSR49/50.TX_POWER clamped to POWER_BOUND.
    # [SRC] 3680-3696 — is_type_ep=false so EP bits stay clear.
    rfcsr = rfcsr_read(t, 49)
    p1 = min(default_power1, POWER_BOUND)
    rfcsr = (rfcsr & ~RFCSR49_TX) | (p1 & RFCSR49_TX)
    rfcsr_write(t, 49, rfcsr & 0xFF)

    rfcsr = rfcsr_read(t, 50)
    p2 = min(default_power2, POWER_BOUND)
    rfcsr = (rfcsr & ~RFCSR50_TX) | (p2 & RFCSR50_TX)
    rfcsr_write(t, 50, rfcsr & 0xFF)

    # ---- (7) RFCSR1 chain power-domain enables. [SRC] 3698-3714 ----
    # RT5592 convention: "PD" bits are active-high CHAIN-ENABLE on this
    # silicon (the naming is misleading — RT3572 inverts the semantic).
    # Confirmed by RT5392 baseline (driver works with all PD bits set
    # for its 1T1R chain). For 2T2R RT5592: TX0+TX1+RX0+RX1 all set.
    rfcsr = rfcsr_read(t, 1)
    rfcsr |= RFCSR1_RF_BLOCK_EN | RFCSR1_PLL_PD
    if tx_chain_num >= 1:
        rfcsr |= RFCSR1_TX0_PD
    else:
        rfcsr &= ~RFCSR1_TX0_PD & 0xFF
    if tx_chain_num == 2:
        rfcsr |= RFCSR1_TX1_PD
    else:
        rfcsr &= ~RFCSR1_TX1_PD & 0xFF
    rfcsr &= ~RFCSR1_TX2_PD & 0xFF
    if rx_chain_num >= 1:
        rfcsr |= RFCSR1_RX0_PD
    else:
        rfcsr &= ~RFCSR1_RX0_PD & 0xFF
    if rx_chain_num == 2:
        rfcsr |= RFCSR1_RX1_PD
    else:
        rfcsr &= ~RFCSR1_RX1_PD & 0xFF
    rfcsr &= ~RFCSR1_RX2_PD & 0xFF
    rfcsr_write(t, 1, rfcsr & 0xFF)

    # ---- (8) RFCSR6 = 0xe4, RFCSR30 = 0x10 (HT20). [SRC] 3715-3720 ----
    rfcsr_write(t, 6, 0xE4)
    rfcsr_write(t, 30, 0x10)

    # ---- (9) Non-11b → RFCSR31/32 = 0x80. [SRC] 3722-3725 ----
    rfcsr_write(t, 31, 0x80)
    rfcsr_write(t, 32, 0x80)

    # ---- (10) Freq trim + VCOCAL kick. [SRC] 3728, 3731-3733 ----
    freq_cal_mode1_usb(t, freq_offset=freq_offset)
    rfcsr = rfcsr_read(t, 3)
    rfcsr |= RFCSR3_VCOCAL_EN
    rfcsr_write(t, 3, rfcsr & 0xFF)

    # ---- (11) BBP62/63/64 NF + BBP79/80/81/82 band-specific values.
    # [SRC] 3736-3743
    nf = (0x37 - (lna_gain & 0xFF)) & 0xFF
    bbp_write(t, 62, nf)
    bbp_write(t, 63, nf)
    bbp_write(t, 64, nf)
    bbp_write(t, 79, 0x1C)
    bbp_write(t, 80, 0x0E)
    bbp_write(t, 81, 0x3A)
    bbp_write(t, 82, 0x62)

    # ---- (12) GLRT band-conditional 6-pair writes. [SRC] 3746-3757 ----
    bbp_glrt_write(t, 128, 0xE0)
    bbp_glrt_write(t, 129, 0x1F)
    bbp_glrt_write(t, 130, 0x38)
    bbp_glrt_write(t, 131, 0x32)
    bbp_glrt_write(t, 133, 0x28)
    bbp_glrt_write(t, 124, 0x19)

    # ---- (13) Post-RF tail of rt2800_config_channel ------------------
    # BBP62/63/64 NF + BBP86=0 (else branch of 4258-4306).
    # The earlier NF writes (step 11) and these are duplicates; the
    # port matches kernel order.
    bbp_write(t, 62, nf)
    bbp_write(t, 63, nf)
    bbp_write(t, 64, nf)
    bbp_write(t, 86, 0x00)

    # BBP82/75 band-specific overwrite. [SRC] 4308-4326 — RT5592 enters
    # the inner branch (not RT5390/RT5392/RT6352). external-LNA-BG splits it:
    # a card with an external 2.4 GHz LNA writes BBP82=0x62 (twice) + BBP75=0x46;
    # otherwise BBP82=0x84 (RT5592 is not RT3593) + BBP75=0x50.
    if has_cap_external_lna_bg:
        bbp_write(t, 82, 0x62)
        bbp_write(t, 82, 0x62)
        bbp_write(t, 75, 0x46)
    else:
        bbp_write(t, 82, 0x84)
        bbp_write(t, 75, 0x50)

    # TX_BAND_CFG — HT20, no A bit, BG=1. [SRC] 4347-4351
    reg = t.read32(TX_BAND_CFG_REG)
    reg &= ~TX_BAND_CFG_HT40_MINUS
    reg &= ~TX_BAND_CFG_A
    reg |= TX_BAND_CFG_BG_BIT
    t.write32(TX_BAND_CFG_REG, reg & 0xFFFFFFFF)

    # TX_PIN_CFG — start from 0 (RT5592 is not RT6352). PAs per
    # tx_chain_num switch (2.4G uses G enables), LNAs per rx_chain_num
    # (both A and G enables set for every active chain — kernel does
    # this for every chip in the post-RF tail). [SRC] 4356-4411
    tx_pin = 0
    if tx_chain_num >= 2:
        tx_pin |= TX_PIN_CFG_PA_PE_G1_EN
    if has_cap_bt_coexist:
        tx_pin |= TX_PIN_CFG_PA_PE_G0_EN_BIT
    else:
        tx_pin |= TX_PIN_CFG_PA_PE_G0_EN_BIT     # 2.4 GHz → G0

    if rx_chain_num >= 2:
        tx_pin |= TX_PIN_CFG_LNA_PE_A1_EN | TX_PIN_CFG_LNA_PE_G1_EN
    tx_pin |= TX_PIN_CFG_LNA_PE_A0_EN_BIT | TX_PIN_CFG_LNA_PE_G0_EN_BIT

    tx_pin |= TX_PIN_CFG_RFTR_EN_BIT | TX_PIN_CFG_TRSW_EN_BIT
    t.write32(TX_PIN_CFG_REG, tx_pin & 0xFFFFFFFF)

    # RT5592-only block. [SRC] 4485-4493
    # BBP141 GLRT = 0x1a (HT20).
    bbp_glrt_write(t, 141, 0x1A)
    # BBP66 AGC = (0x1c for 2.4G) + 2*lna_gain, fanned across RX chains.
    bbp66 = (0x1C + 2 * (lna_gain & 0xFF)) & 0xFF
    bbp_write_with_rx_chain(t, 66, bbp66, rx_chain_num=rx_chain_num)
    # rt2800_iq_calibrate — per-tune IQ trim from EEPROM.
    iq_calibrate(t, channel, iq_cal)

    # BBP4 BANDWIDTH = 0 (HT20). [SRC] 4526-4528
    bbp = bbp_read(t, 4)
    bbp &= ~BBP4_BANDWIDTH
    bbp_write(t, 4, bbp & 0xFF)

    # BBP3 HT40_MINUS = 0 (HT20). [SRC] 4530-4532
    bbp = bbp_read(t, 3)
    bbp &= ~BBP3_HT40_MINUS
    bbp_write(t, 3, bbp & 0xFF)

    time.sleep(0.001)

    # Clear channel-activity counters as a side-effect of reading.
    t.read32(CH_IDLE_STA)
    t.read32(CH_BUSY_STA)
    t.read32(CH_BUSY_STA_SEC)

    logger.debug(
        "set_channel_5592: ch=%d N=%d K=%d mod=%d R=%d "
        "tx/rx_chain=(%d,%d) lna_gain=%d freq_off=%d bbp66=0x%02x",
        channel, n, k, mod, r,
        tx_chain_num, rx_chain_num, lna_gain, freq_offset, bbp66,
    )


# ----------------------------------------------------------------------
# RT5572 / RF5592 5 GHz channel tune.
#
# Mirrors the `rf->channel > 14` branch of rt2800_config_channel_rf55xx
# (rt2800lib.c:3573-3677) plus the 5 GHz post-RF tail from
# rt2800_config_channel (4328-4345, 4485-4493). Three sub-bands with
# overlapping per-channel breakpoint tweaks — UNII-1/2 (ch 36-64),
# UNII-2-ext (ch 100-138), UNII-3 (ch 140-165). Kernel comments call
# out half-channel offsets (38/42/46/50/...) that the table includes
# for 20/40-MHz secondary-channel pairing; we tune them the same way.
# ----------------------------------------------------------------------
def _set_channel_5592_5g(
    t: RT5572Transport,
    channel: int,
    *,
    n: int,
    k: int,
    mod: int,
    r: int,
    freq_offset: int = 0,
    lna_gain: int = 0,
    tx_chain_num: int = 2,
    rx_chain_num: int = 2,
    has_cap_external_lna_a: bool = False,
    default_power1: int = 0,
    default_power2: int = 0,
    iq_cal: IqCalChannel | None = None,
) -> None:
    # ---- (1) LDO_CFG0 — VLEVEL=5 for HT20 5 GHz. [SRC] 3498-3501 ----
    reg = t.read32(LDO_CFG0)
    reg = (reg & ~LDO_CFG0_LDO_CORE_VLEVEL) | ((5 << 26) & LDO_CFG0_LDO_CORE_VLEVEL)
    t.write32(LDO_CFG0, reg & 0xFFFFFFFF)

    # ---- (2) Synthesizer: RFCSR8/9/11 packed from {N, K, mod, R} ----
    # Identical to 2.4 GHz path. [SRC] 3504-3515
    rfcsr_write(t, 8, n & 0xFF)

    rfcsr = rfcsr_read(t, 9)
    rfcsr = (rfcsr & ~RFCSR9_K) | (k & RFCSR9_K)
    rfcsr = (rfcsr & ~RFCSR9_N) | (((n & 0x100) >> 8 << 4) & RFCSR9_N)
    rfcsr = (rfcsr & ~RFCSR9_MOD) | ((((mod - 8) & 0x4) >> 2 << 7) & RFCSR9_MOD)
    rfcsr_write(t, 9, rfcsr & 0xFF)

    rfcsr = rfcsr_read(t, 11)
    rfcsr = (rfcsr & ~RFCSR11_R) | ((r - 1) & RFCSR11_R)
    rfcsr = (rfcsr & ~RFCSR11_MOD) | (((mod - 8) & 0x3) << 6 & RFCSR11_MOD)
    rfcsr_write(t, 11, rfcsr & 0xFF)

    # ---- (3) 5 GHz fixed-value RFCSR block. [SRC] 3574-3589 ----
    # Kernel comment: "FIMXE: RF11 overwrite" — clobbers synthesizer
    # bits from step 2 (ported as-is).
    rfcsr_write(t, 10, 0x97)
    rfcsr_write(t, 11, 0x40)
    rfcsr_write(t, 25, 0xBF)
    rfcsr_write(t, 27, 0x42)
    rfcsr_write(t, 36, 0x00)
    rfcsr_write(t, 37, 0x04)
    rfcsr_write(t, 38, 0x85)
    rfcsr_write(t, 40, 0x42)
    rfcsr_write(t, 41, 0xBB)
    rfcsr_write(t, 42, 0xD7)
    rfcsr_write(t, 45, 0x41)
    rfcsr_write(t, 48, 0x00)
    rfcsr_write(t, 57, 0x77)
    rfcsr_write(t, 60, 0x05)
    rfcsr_write(t, 61, 0x01)

    # ---- (4) Sub-band conditionals. ----
    if 36 <= channel <= 64:
        # UNII-1/2 block. [SRC] 3593-3620
        rfcsr_write(t, 12, 0x2E)
        rfcsr_write(t, 13, 0x22)
        rfcsr_write(t, 22, 0x60)
        rfcsr_write(t, 23, 0x7F)
        rfcsr_write(t, 24, 0x09 if channel <= 50 else 0x07)
        rfcsr_write(t, 39, 0x1C)
        rfcsr_write(t, 43, 0x5B)
        rfcsr_write(t, 44, 0x40)
        rfcsr_write(t, 46, 0x00)
        rfcsr_write(t, 51, 0xFE)
        rfcsr_write(t, 52, 0x0C)
        rfcsr_write(t, 54, 0xF8)
        if channel <= 50:
            rfcsr_write(t, 55, 0x06)
            rfcsr_write(t, 56, 0xD3)
        else:
            rfcsr_write(t, 55, 0x04)
            rfcsr_write(t, 56, 0xBB)
        rfcsr_write(t, 58, 0x15)
        rfcsr_write(t, 59, 0x7F)
        rfcsr_write(t, 62, 0x15)
    elif 100 <= channel <= 165:
        # UNII-2-ext + UNII-3 block. [SRC] 3622-3673
        rfcsr_write(t, 12, 0x0E)
        rfcsr_write(t, 13, 0x42)
        rfcsr_write(t, 22, 0x40)
        if channel <= 153:
            rfcsr_write(t, 23, 0x3C)
            rfcsr_write(t, 24, 0x06)
        else:
            rfcsr_write(t, 23, 0x38)
            rfcsr_write(t, 24, 0x05)
        if channel <= 138:
            rfcsr_write(t, 39, 0x1A)
            rfcsr_write(t, 43, 0x3B)
            rfcsr_write(t, 44, 0x20)
            rfcsr_write(t, 46, 0x18)
        else:
            rfcsr_write(t, 39, 0x18)
            rfcsr_write(t, 43, 0x1B)
            rfcsr_write(t, 44, 0x10)
            rfcsr_write(t, 46, 0x08)
        rfcsr_write(t, 51, 0xFC if channel <= 124 else 0xEC)
        # Kernel writes RFCSR52=0x06 on both sides of the 138/140
        # split (same value), so this is unconditional.
        rfcsr_write(t, 52, 0x06)
        rfcsr_write(t, 54, 0xEB)
        rfcsr_write(t, 55, 0x01 if channel <= 138 else 0x00)
        rfcsr_write(t, 56, 0xBB if channel <= 128 else 0xAB)
        rfcsr_write(t, 58, 0x1D if channel <= 116 else 0x15)
        rfcsr_write(t, 59, 0x3F if channel <= 138 else 0x7C)
        rfcsr_write(t, 62, 0x1D if channel <= 116 else 0x15)
    else:
        raise ValueError(
            f"channel {channel} outside RF5592 5 GHz sub-bands (36-64 + 100-165)"
        )

    # ---- (5) TX power: RFCSR49/50.TX_POWER clamped to POWER_BOUND_5G.
    # [SRC] 3680-3696 — is_type_ep=false so EP bits stay clear.
    rfcsr = rfcsr_read(t, 49)
    p1 = min(default_power1, POWER_BOUND_5G)
    rfcsr = (rfcsr & ~RFCSR49_TX) | (p1 & RFCSR49_TX)
    rfcsr_write(t, 49, rfcsr & 0xFF)

    rfcsr = rfcsr_read(t, 50)
    p2 = min(default_power2, POWER_BOUND_5G)
    rfcsr = (rfcsr & ~RFCSR50_TX) | (p2 & RFCSR50_TX)
    rfcsr_write(t, 50, rfcsr & 0xFF)

    # ---- (6) RFCSR1 chain power-domain enables. [SRC] 3698-3714 ----
    # Same as 2.4G — 2T2R: TX0+TX1+RX0+RX1 active, TX2/RX2 off.
    # (No BT coex on 5 GHz so the bt_coexist branch is irrelevant.)
    rfcsr = rfcsr_read(t, 1)
    rfcsr |= RFCSR1_RF_BLOCK_EN | RFCSR1_PLL_PD
    if tx_chain_num >= 1:
        rfcsr |= RFCSR1_TX0_PD
    else:
        rfcsr &= ~RFCSR1_TX0_PD & 0xFF
    if tx_chain_num == 2:
        rfcsr |= RFCSR1_TX1_PD
    else:
        rfcsr &= ~RFCSR1_TX1_PD & 0xFF
    rfcsr &= ~RFCSR1_TX2_PD & 0xFF
    if rx_chain_num >= 1:
        rfcsr |= RFCSR1_RX0_PD
    else:
        rfcsr &= ~RFCSR1_RX0_PD & 0xFF
    if rx_chain_num == 2:
        rfcsr |= RFCSR1_RX1_PD
    else:
        rfcsr &= ~RFCSR1_RX1_PD & 0xFF
    rfcsr &= ~RFCSR1_RX2_PD & 0xFF
    rfcsr_write(t, 1, rfcsr & 0xFF)

    # ---- (7) RFCSR6 = 0xe4, RFCSR30 = 0x10 (HT20). [SRC] 3715-3720 ----
    rfcsr_write(t, 6, 0xE4)
    rfcsr_write(t, 30, 0x10)

    # ---- (8) Non-11b → RFCSR31/32 = 0x80. [SRC] 3722-3725 ----
    rfcsr_write(t, 31, 0x80)
    rfcsr_write(t, 32, 0x80)

    # ---- (9) Freq trim + VCOCAL kick. [SRC] 3728, 3731-3733 ----
    freq_cal_mode1_usb(t, freq_offset=freq_offset)
    rfcsr = rfcsr_read(t, 3)
    rfcsr |= RFCSR3_VCOCAL_EN
    rfcsr_write(t, 3, rfcsr & 0xFF)

    # ---- (10) BBP62/63/64 NF + BBP79/80/81/82 5 GHz values.
    # [SRC] 3736-3743
    nf = (0x37 - (lna_gain & 0xFF)) & 0xFF
    bbp_write(t, 62, nf)
    bbp_write(t, 63, nf)
    bbp_write(t, 64, nf)
    bbp_write(t, 79, 0x18)
    bbp_write(t, 80, 0x08)
    bbp_write(t, 81, 0x38)
    bbp_write(t, 82, 0x92)

    # ---- (11) GLRT band-conditional 6-pair writes. [SRC] 3746-3757 ----
    # 5 GHz values from the (rf->channel <= 14 ? : ) ternaries.
    bbp_glrt_write(t, 128, 0xF0)
    bbp_glrt_write(t, 129, 0x1E)
    bbp_glrt_write(t, 130, 0x28)
    bbp_glrt_write(t, 131, 0x20)
    bbp_glrt_write(t, 133, 0x7F)
    bbp_glrt_write(t, 124, 0x7F)

    # ---- (12) Post-RF tail of rt2800_config_channel ------------------
    # BBP62/63/64 NF re-write + BBP86=0 (else branch of 4258-4306).
    bbp_write(t, 62, nf)
    bbp_write(t, 63, nf)
    bbp_write(t, 64, nf)
    bbp_write(t, 86, 0x00)

    # BBP82/75 5 GHz overwrite. [SRC] 4328-4345 — RT5592 enters the
    # !RT3572 && !RT3593/3883 && !RT6352 branch → BBP82 = 0xf2.
    # BBP75 depends on has_cap_external_lna_a.
    bbp_write(t, 82, 0xF2)
    bbp_write(t, 75, 0x46 if has_cap_external_lna_a else 0x50)

    # TX_BAND_CFG — HT20, A=1 (5 GHz), BG=0. [SRC] 4347-4351
    reg = t.read32(TX_BAND_CFG_REG)
    reg &= ~TX_BAND_CFG_HT40_MINUS
    reg |= TX_BAND_CFG_A
    reg &= ~TX_BAND_CFG_BG_BIT
    t.write32(TX_BAND_CFG_REG, reg & 0xFFFFFFFF)

    # TX_PIN_CFG — start from 0 (RT5592 is not RT6352). PAs use A-side
    # for 5 GHz. LNAs set BOTH A- and G-side enables per chain (kernel
    # does this regardless of band). [SRC] 4356-4411
    tx_pin = 0
    if tx_chain_num >= 2:
        tx_pin |= TX_PIN_CFG_PA_PE_A1_EN
    tx_pin |= TX_PIN_CFG_PA_PE_A0_EN_BIT

    if rx_chain_num >= 2:
        tx_pin |= TX_PIN_CFG_LNA_PE_A1_EN | TX_PIN_CFG_LNA_PE_G1_EN
    tx_pin |= TX_PIN_CFG_LNA_PE_A0_EN_BIT | TX_PIN_CFG_LNA_PE_G0_EN_BIT

    tx_pin |= TX_PIN_CFG_RFTR_EN_BIT | TX_PIN_CFG_TRSW_EN_BIT
    t.write32(TX_PIN_CFG_REG, tx_pin & 0xFFFFFFFF)

    # RT5592-only block. [SRC] 4485-4493
    bbp_glrt_write(t, 141, 0x1A)
    # BBP66 AGC: 0x24 for 5G (vs 0x1c for 2.4G) + 2*lna_gain.
    bbp66 = (0x24 + 2 * (lna_gain & 0xFF)) & 0xFF
    bbp_write_with_rx_chain(t, 66, bbp66, rx_chain_num=rx_chain_num)
    iq_calibrate(t, channel, iq_cal)

    # BBP4 BANDWIDTH = 0 (HT20). [SRC] 4526-4528
    bbp = bbp_read(t, 4)
    bbp &= ~BBP4_BANDWIDTH
    bbp_write(t, 4, bbp & 0xFF)

    # BBP3 HT40_MINUS = 0 (HT20). [SRC] 4530-4532
    bbp = bbp_read(t, 3)
    bbp &= ~BBP3_HT40_MINUS
    bbp_write(t, 3, bbp & 0xFF)

    time.sleep(0.001)

    # Clear channel-activity counters as a side-effect of reading.
    t.read32(CH_IDLE_STA)
    t.read32(CH_BUSY_STA)
    t.read32(CH_BUSY_STA_SEC)

    logger.debug(
        "set_channel_5592_5g: ch=%d N=%d K=%d mod=%d R=%d "
        "tx/rx_chain=(%d,%d) lna_gain=%d freq_off=%d bbp66=0x%02x ext_lna_a=%s",
        channel, n, k, mod, r,
        tx_chain_num, rx_chain_num, lna_gain, freq_offset, bbp66,
        has_cap_external_lna_a,
    )


# ----------------------------------------------------------------------
# Per-channel + per-rate TX power (EEPROM-derived).
#
# The kernel builds a channel-info array once (rt2800lib.c:11923-11957):
# 2.4 GHz power1/2 = EEPROM TXPOWER_BG1/BG2[ch-1]; 5 GHz = TXPOWER_A1/A2
# indexed by the channel's position AFTER the 14 2.4 GHz channels in the
# per-silicon RF channel table. Each config_channel then clamps via
# rt2800_txpower_to_dev before writing RFCSR49/50 (RF55xx) or RFCSR12/13
# (RF3052). The 5 GHz index is per-silicon: RF5592 walks the (larger,
# half-channel-dense) rf_vals_5592 table, RF3052 walks rf_vals_3x — so the
# same 5 GHz channel maps to different A1/A2 bytes on the two silicons.
# ----------------------------------------------------------------------
def txpower_5g_index(silicon_id: int, channel: int, xtal_40mhz: bool = False) -> int:
    """Index into the EEPROM TXPOWER_A1/A2 arrays for a 5 GHz ``channel``:
    its 0-based position among the >14 channels of this silicon's RF table
    (== kernel ``i - 14``). [SRC] rt2800lib.c:11952-11957."""
    if silicon_id == RT_RT5592:
        table = _RF_VALS_5592_XTAL40 if xtal_40mhz else _RF_VALS_5592_XTAL20
    elif silicon_id == RT_RT3572:
        table = _RF_VALS_3X
    else:
        raise NotImplementedError(f"5 GHz TX-power index for silicon 0x{silicon_id:04x}")
    return [ch for ch in table if ch > 14].index(channel)


def default_power(ev: EepromValues, silicon_id: int, channel: int,
                  xtal_40mhz: bool = False) -> tuple[int, int]:
    """Per-channel (chain-0, chain-1) TX power decoded from a BURNED EEPROM and
    clamped to the device range. Callers gate on ``ev.looks_unburned`` first —
    an unburned EEPROM has no calibration to decode. [SRC] rt2800lib.c:4170-4177
    + 11923-11957."""
    if channel <= 14:
        p1 = txpower_to_dev(channel, ev.power_byte(EEPROM_OFFSET_TXPOWER_BG1, channel - 1))
        p2 = txpower_to_dev(channel, ev.power_byte(EEPROM_OFFSET_TXPOWER_BG2, channel - 1))
    else:
        idx = txpower_5g_index(silicon_id, channel, xtal_40mhz)
        p1 = txpower_to_dev(channel, ev.power_byte(EEPROM_OFFSET_TXPOWER_A1, idx))
        p2 = txpower_to_dev(channel, ev.power_byte(EEPROM_OFFSET_TXPOWER_A2, idx))
    return p1, p2


_TX_PWR_CFG_REGS = (TX_PWR_CFG_0, TX_PWR_CFG_1, TX_PWR_CFG_2, TX_PWR_CFG_3, TX_PWR_CFG_4)


def _compensate_txpower(ev: EepromValues, is_2g: bool, is_rate_b: bool,
                        txpower: int, delta: int) -> int:
    """Per-rate TX-power compensation [SRC rt2800lib.c:4748-4797
    rt2800_compensate_txpower], RT5592 (neither RT3593 nor RT3883). With
    CAPABILITY_POWER_LIMIT clear, reg_limit collapses to 0 and this is just
    ``min(txpower + delta, 0xC)`` clamped at 0. power_level is the regulatory
    max in monitor mode, so it is 0 here."""
    if ev.power_limit:
        eirp_word = ev.word(EEPROM_OFFSET_EIRP_MAX_TX_POWER)
        eirp_mask = EEPROM_EIRP_MAX_TX_POWER_2GHZ if is_2g else EEPROM_EIRP_MAX_TX_POWER_5GHZ
        eirp_shift = 0 if is_2g else 8
        eirp_criterion = (eirp_word & eirp_mask) >> eirp_shift
        # OFDM-6M criterion = RATE0 of the 2nd BYRATE word.
        criterion = ev.word(EEPROM_OFFSET_TXPOWER_BYRATE + 1) & 0x000F
        power_level = 0
        eirp = eirp_criterion + (txpower - criterion) + (4 if is_rate_b else 0) + delta
        reg_limit = (eirp - power_level) if eirp > power_level else 0
    else:
        reg_limit = 0
    return min(max(0, txpower + delta - reg_limit), 0xC)


def config_ant(t: RT5572Transport, tx_chain_num: int, rx_chain_num: int) -> None:
    """rt2800_config_ant — program the TX/RX antenna-chain selects into
    BBP1.TX_ANTENNA + BBP3.RX_ANTENNA, writing BBP3 before BBP1. This is the
    RT5592 path (no RT3572 BT-coexist branch, no RT3593/RT3883 BBP86). Called
    from the antenna-config path with the RX quiesced. [SRC] rt2800lib.c:2322."""
    r1 = bbp_read(t, 1)
    r3 = bbp_read(t, 3)

    # TX antenna: 1 chain -> field 0; 2 or 3 chains -> field 2.
    tx_field = 0 if tx_chain_num == 1 else 2
    r1 = (r1 & ~BBP1_TX_ANTENNA) | ((tx_field << 3) & BBP1_TX_ANTENNA)

    # RX antenna: 1 -> 0, 2 -> 1, 3 -> 2.
    rx_field = {1: 0, 2: 1, 3: 2}[rx_chain_num]
    r3 = (r3 & ~BBP3_RX_ANTENNA) | ((rx_field << 3) & BBP3_RX_ANTENNA)

    bbp_write(t, 3, r3 & 0xFF)
    bbp_write(t, 1, r1 & 0xFF)


def config_txpower(t: RT5572Transport, ev: EepromValues, is_2g: bool) -> None:
    """Per-rate TX power → BBP1.TX_POWER_CTRL + TX_PWR_CFG_0..4, from the EEPROM
    TXPOWER_BYRATE table [SRC rt2800lib.c:5338-5519 rt2800_config_txpower_rt28xx].
    RF55xx (RT5592) path: the gain-calibration delta is RT3070/71/90/3572-only
    (RT5592 falls in the default case → 0), HT20 bw-comp = 0, and monitor tunes
    to the regulatory max so reg_delta = 0 → total delta = 0. Same kernel
    function the (verified) chips/rt5370 port covers for 2.4 GHz."""
    delta = 0
    # delta > -6 → BBP1.TX_POWER_CTRL = 0 (no -6/-12 dBm backoff).
    bbp1 = bbp_read(t, 1)
    bbp1 = bbp1 & ~0x03            # BBP1_TX_POWER_CTRL bits[1:0] = 0
    bbp_write(t, 1, bbp1 & 0xFF)

    for reg_i, i in enumerate(range(0, EEPROM_TXPOWER_BYRATE_SIZE, 2)):
        offset = _TX_PWR_CFG_REGS[reg_i]
        reg = t.read32(offset)
        lo = ev.word(EEPROM_OFFSET_TXPOWER_BYRATE + i)
        hi = ev.word(EEPROM_OFFSET_TXPOWER_BYRATE + i + 1)
        for k in range(4):
            tp = _compensate_txpower(ev, is_2g, i == 0, (lo >> (4 * k)) & 0xF, delta)
            reg = (reg & ~(0xF << (4 * k))) | (tp << (4 * k))
        for k in range(4):
            tp = _compensate_txpower(ev, is_2g, False, (hi >> (4 * k)) & 0xF, delta)
            reg = (reg & ~(0xF << (4 * (4 + k)))) | (tp << (4 * (4 + k)))
        t.write32(offset, reg & 0xFFFFFFFF)


# ----------------------------------------------------------------------
# Public dispatcher.
# ----------------------------------------------------------------------
def set_channel(
    t: RT5572Transport,
    silicon_id: int,
    channel: int,
    *,
    freq_offset: int = 0,
    lna_gain: int = 0,
    cal_result: Optional[RfFilterCal] = None,
    txmixer_gain_24g: int = 0,
    txmixer_gain_5g: int = 0,
    tx_chain_num: int = 1,
    rx_chain_num: int = 1,
    has_cap_bt_coexist: bool = False,
    has_cap_external_lna_a: bool = False,
    has_cap_external_lna_bg: bool = False,
    default_power1: int = 0,
    default_power2: int = 0,
    xtal_40mhz: bool = False,
    iq_cal: IqCalibration | None = None,
    eeprom: EepromValues | None = None,
) -> None:
    """Tune to ``channel`` on the given silicon.

    All EEPROM-derived kwargs default to "0 / no" so existing call
    sites still work for RT5392 (which ignores everything except
    ``freq_offset`` and ``lna_gain``). RT3572 needs the cal_result
    plus chain counts; pass them in or the call will fail.
    """
    if silicon_id == RT_RT5392:
        if channel not in _RF_VALS_3X:
            raise ValueError(
                f"channel {channel} not in rf_vals_3x table (valid: 1..14, "
                "36-173 per kernel rt2800lib.c:11435-11494)"
            )
        if channel > 14:
            raise ValueError(
                f"RT5392 is 2.4 GHz only; channel {channel} not supported"
            )
        _set_channel_5392(t, channel, freq_offset=freq_offset, lna_gain=lna_gain)
    elif silicon_id == RT_RT3572:
        if channel not in _RF_VALS_3X:
            raise ValueError(
                f"channel {channel} not in rf_vals_3x table (valid: 1..14, "
                "36-173 per kernel rt2800lib.c:11435-11494)"
            )
        _set_channel_3572(
            t, channel,
            freq_offset=freq_offset,
            lna_gain=lna_gain,
            cal_result=cal_result,
            txmixer_gain_24g=txmixer_gain_24g,
            txmixer_gain_5g=txmixer_gain_5g,
            tx_chain_num=tx_chain_num,
            rx_chain_num=rx_chain_num,
            has_cap_bt_coexist=has_cap_bt_coexist,
            has_cap_external_lna_a=has_cap_external_lna_a,
            default_power1=default_power1,
            default_power2=default_power2,
        )
    elif silicon_id == RT_RT5592:
        table = _RF_VALS_5592_XTAL40 if xtal_40mhz else _RF_VALS_5592_XTAL20
        if channel not in table:
            raise ValueError(
                f"RT5592 channel {channel} not in rf_vals_5592_xtal{'40' if xtal_40mhz else '20'}"
            )
        n, k, mod, r = table[channel]
        iq_for_ch = iq_cal.for_channel(channel) if iq_cal is not None else None
        if channel <= 14:
            _set_channel_5592_2g(
                t, channel,
                n=n, k=k, mod=mod, r=r,
                freq_offset=freq_offset,
                lna_gain=lna_gain,
                tx_chain_num=tx_chain_num,
                rx_chain_num=rx_chain_num,
                has_cap_bt_coexist=has_cap_bt_coexist,
                has_cap_external_lna_bg=has_cap_external_lna_bg,
                default_power1=default_power1,
                default_power2=default_power2,
                iq_cal=iq_for_ch,
            )
        else:
            _set_channel_5592_5g(
                t, channel,
                n=n, k=k, mod=mod, r=r,
                freq_offset=freq_offset,
                lna_gain=lna_gain,
                tx_chain_num=tx_chain_num,
                rx_chain_num=rx_chain_num,
                has_cap_external_lna_a=has_cap_external_lna_a,
                default_power1=default_power1,
                default_power2=default_power2,
                iq_cal=iq_for_ch,
            )
        # Per-rate TX power (config_txpower) runs right after config_channel in
        # the kernel's CHANGE_CHANNEL sequence. The RF55xx tune above only sets
        # the analog PA gain (RFCSR49/50); without this the baseband per-rate
        # power (TX_PWR_CFG_0..4) is left at its init value. [SRC] rt2800lib.c:
        # 5695 rt2800_config → config_txpower after config_channel.
        if eeprom is not None:
            config_txpower(t, eeprom, is_2g=channel <= 14)
    else:
        raise NotImplementedError(
            f"set_channel for silicon 0x{silicon_id:04x} not yet validated"
        )


def reconfig_channel(t: RT5572Transport, silicon_id: int, channel: int,
                     **sc_kwargs) -> None:
    """The RF reconfiguration for a channel change, RX assumed already quiesced:

        config_channel + config_txpower  →  reset_tuner  →  config_ant  →  reset_tuner

    This is the shared per-hop core. ``Driver.set_channel`` brackets it with the
    load-bearing RX toggle (stop_queue → reconfig → start_queue) — the kernel
    disables RX around config_channel or the RF/BBP writes don't latch, which is
    the focus-mode dead-radio / band-transition bug. The acceptance gate drives
    THIS function per hop, so the reconfig sequence is byte-checked against the
    kernel. [SRC] rt2x00mac_config (config → reset_tuner) + rt2x00lib_config_antenna
    (config_ant → reset_tuner), rt2x00config.c."""
    from .link_tuner import get_default_vgc, set_vgc
    eeprom = sc_kwargs["eeprom"]
    vgc = get_default_vgc(silicon_id, channel, sc_kwargs["lna_gain"])
    set_channel(t, silicon_id, channel, **sc_kwargs)                    # config_channel+txpower
    set_vgc(t, silicon_id, vgc, rx_chain_num=eeprom.rxpath, rssi=0)     # reset_tuner
    config_ant(t, eeprom.txpath, eeprom.rxpath)                        # config_antenna
    set_vgc(t, silicon_id, vgc, rx_chain_num=eeprom.rxpath, rssi=0)     # reset_tuner
