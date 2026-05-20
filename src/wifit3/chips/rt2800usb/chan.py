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

from .bbp import bbp_read, bbp_write, bbp_write_with_rx_chain
from .constants import (
    BBP3_HT40_MINUS,
    BBP4_BANDWIDTH,
    CH_BUSY_STA,
    CH_BUSY_STA_SEC,
    CH_IDLE_STA,
    GPIO_CTRL,
    GPIO_CTRL_DIR7,
    GPIO_CTRL_VAL7,
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
    RFCSR11_R,
    RFCSR12_DR0,
    RFCSR12_TX_POWER,
    RFCSR13_DR0,
    RFCSR13_TX_POWER,
    RFCSR16_TXMIXER_GAIN,
    RFCSR23_FREQ_OFFSET,
    RFCSR30_RX_H20M,
    RFCSR30_TX_H20M,
    RT_RT3572,
    RT_RT5392,
    TX_BAND_CFG_A,
    TX_BAND_CFG_BG_BIT,
    TX_BAND_CFG_HT40_MINUS,
    TX_BAND_CFG_REG,
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
from .firmware import mcu_request
from .rfcsr import RfFilterCal, rfcsr_read, rfcsr_write
from .transport import RT2800USBTransport

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

# MCU command for freq offset update on USB.  [SRC] rt2800.h
MCU_FREQ_OFFSET = 0x74


def freq_cal_mode1_usb(t: RT2800USBTransport, freq_offset: int = 0) -> None:
    """USB version of freq_cal_mode1 (rt2800lib.c:2447-2480).

    Only RF53xx silicon uses this — RF3052 writes freq_offset directly
    to RFCSR23 inside its config_channel function.
    """
    rfcsr17 = rfcsr_read(t, 17)
    mcu_request(
        t, MCU_FREQ_OFFSET,
        token=0xFF, arg0=freq_offset & 0xFF, arg1=rfcsr17 & 0xFF,
    )


# ----------------------------------------------------------------------
# RF53xx 2.4 GHz channel tune (RT5390/RT5392).
# Body unchanged from M-A1; uses the shared `_RF_VALS_3X` table.
# RF53xx silicon is 2.4 GHz only — caller must not pass channels > 14.
# ----------------------------------------------------------------------
def _set_channel_5392(
    t: RT2800USBTransport,
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
    t: RT2800USBTransport,
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
# Public dispatcher.
# ----------------------------------------------------------------------
def set_channel(
    t: RT2800USBTransport,
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
    default_power1: int = 0,
    default_power2: int = 0,
) -> None:
    """Tune to ``channel`` on the given silicon.

    All EEPROM-derived kwargs default to "0 / no" so existing call
    sites still work for RT5392 (which ignores everything except
    ``freq_offset`` and ``lna_gain``). RT3572 needs the cal_result
    plus chain counts; pass them in or the call will fail.
    """
    if channel not in _RF_VALS_3X:
        raise ValueError(
            f"channel {channel} not in rf_vals_3x table (valid: 1..14, "
            "36-173 per kernel rt2800lib.c:11435-11494)"
        )
    if silicon_id == RT_RT5392:
        if channel > 14:
            raise ValueError(
                f"RT5392 is 2.4 GHz only; channel {channel} not supported"
            )
        _set_channel_5392(t, channel, freq_offset=freq_offset, lna_gain=lna_gain)
    elif silicon_id == RT_RT3572:
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
    else:
        raise NotImplementedError(
            f"set_channel for silicon 0x{silicon_id:04x} not yet validated"
        )
