"""rt2800usb 2.4 GHz channel tune.

Mirrors the kernel's `rt2800_config_channel` dispatcher (rt2800lib.c:4161)
for the chips we support. The dispatcher branches by RF chip, NOT by
silicon — the same silicon ID can pair with different RF chips on
some platforms — but for the dongles wifit3 claims:

  silicon 0x5392 (RT5372 / Panda PAU05) → RF53xx code path
  silicon 0x3572 (RT3572 / AWUS051NH v2) → RF3052 code path
  silicon 0x5592 (RT5572 / Panda PAU09)  → RF55xx code path  (M-B2 TBD)

Channels 1..14 only for now — 5 GHz needs the RF chip's 5G branch +
EEPROM TX power tables. M-A2 (RT3572 5 GHz) is the next milestone.

[SRC] rt2800lib.c:2547-2795 (config_channel_rf3xxx + config_channel_rf3052)
      rt2800lib.c:3387-3483 (config_channel_rf53xx)
      rt2800lib.c:4161-4563 (config_channel dispatcher + post-RF block)
      rt2800lib.c:11435-11449 (rf_vals_3x table, 2.4 GHz portion)
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
# Channels 1..14 from rt2800lib.c:11435-11449 (rf_vals_3x).
# Used by BOTH the RF53xx and RF3052 code paths — kernel
# `rt2800_probe_hw_mode` sets spec->channels = rf_vals_3x for these
# chips' 2.4 GHz spec.
# Tuple format: (rf1, rf2, rf3). For RF53xx these go to RFCSR8/11_R/9;
# for RF3052 they go to RFCSR2/6_R1/3.
# ----------------------------------------------------------------------
_RF_VALS_2G = {
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
}

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
# Body unchanged from M-now; signature reshaped for the new dispatcher.
# ----------------------------------------------------------------------
def _set_channel_5392(
    t: RT2800USBTransport,
    channel: int,
    *,
    freq_offset: int = 0,
    lna_gain: int = 0,
) -> None:
    rf1, rf2, rf3 = _RF_VALS_2G[channel]

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
# RT3572 / RF3052 2.4 GHz channel tune.
#
# Bigger than RF53xx because:
#   * synthesizer uses RFCSR2/3/6 (not 8/9/11) with a TX divider field
#   * RFCSR5_R1 / RFCSR1 powerdown bits / RFCSR23 freq_offset are all
#     written direct (no MCU command)
#   * the 2.4 GHz branch programs ~14 RFCSR values into a fresh state
#   * GPIO_CTRL bit 7 acts as the band-switch hardware signal
#   * the post-RF block writes BBP62-64/75/82/86 + tx_pin from
#     EEPROM-derived chain counts + the RT3572-only RFCSR8 toggle + an
#     AGC init across each RX chain
#
# [SRC] rt2800lib.c:2625-2795 (config_channel_rf3052)
#       rt2800lib.c:4257-4546 (post-RF tail of config_channel)
# ----------------------------------------------------------------------
def _set_channel_3572(
    t: RT2800USBTransport,
    channel: int,
    *,
    freq_offset: int = 0,
    lna_gain: int = 0,
    txmixer_gain_24g: int = 0,
    tx_chain_num: int = 2,
    rx_chain_num: int = 2,
    has_cap_bt_coexist: bool = False,
    cal_result: Optional[RfFilterCal] = None,
    default_power1: int = 0,
    default_power2: int = 0,
) -> None:
    if cal_result is None:
        raise ValueError(
            "_set_channel_3572 requires cal_result from init_rfcsr_3572"
        )

    rf1, rf2, rf3 = _RF_VALS_2G[channel]

    # ---- (1) Restore BBP25/26 from init-time calibration capture ----
    bbp_write(t, 25, cal_result.bbp25)
    bbp_write(t, 26, cal_result.bbp26)

    # ---- (2) Synthesizer + dividers (RF3052 layout) -----------------
    rfcsr_write(t, 2, rf1)
    rfcsr_write(t, 3, rf3)

    rfcsr = rfcsr_read(t, 6)
    rfcsr = (rfcsr & ~RFCSR6_R1) | (rf2 & RFCSR6_R1)
    rfcsr = (rfcsr & ~RFCSR6_TXDIV) | ((2 << 2) & RFCSR6_TXDIV)   # 2.4G → 2
    rfcsr_write(t, 6, rfcsr & 0xFF)

    rfcsr = rfcsr_read(t, 5)
    rfcsr = (rfcsr & ~RFCSR5_R1) | ((1 << 2) & RFCSR5_R1)         # 2.4G → 1
    rfcsr_write(t, 5, rfcsr & 0xFF)

    # ---- (3) TX power (RFCSR12/13) ----------------------------------
    rfcsr = rfcsr_read(t, 12)
    rfcsr = (rfcsr & ~RFCSR12_DR0) | ((3 << 5) & RFCSR12_DR0)
    rfcsr = (rfcsr & ~RFCSR12_TX_POWER) | (default_power1 & RFCSR12_TX_POWER)
    rfcsr_write(t, 12, rfcsr & 0xFF)

    rfcsr = rfcsr_read(t, 13)
    rfcsr = (rfcsr & ~RFCSR13_DR0) | ((3 << 5) & RFCSR13_DR0)
    rfcsr = (rfcsr & ~RFCSR13_TX_POWER) | (default_power2 & RFCSR13_TX_POWER)
    rfcsr_write(t, 13, rfcsr & 0xFF)

    # ---- (4) RFCSR1 chain power-downs -------------------------------
    rfcsr = rfcsr_read(t, 1)
    # Start with all six PD bits cleared.
    pd_mask = (
        RFCSR1_RX0_PD | RFCSR1_TX0_PD
        | RFCSR1_RX1_PD | RFCSR1_TX1_PD
        | RFCSR1_RX2_PD | RFCSR1_TX2_PD
    )
    rfcsr &= ~pd_mask & 0xFF

    if has_cap_bt_coexist:
        # Kernel: 2.4G + BT coex powers down the primary chains to share
        # the antenna with BT; we don't have BT coex without EEPROM so
        # this branch never fires on AWUS051NH v2 today.
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
    # HT20 monitor only — always use calibration_bw20.
    rfcsr_write(t, 24, cal_result.calibration_bw20)
    rfcsr_write(t, 31, cal_result.calibration_bw20)

    # ---- (7) RF3052 2.4 GHz block ----------------------------------
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

    # ---- (8) GPIO_CTRL band-switch (GPIO #7 = 1 → 2.4G) -------------
    reg = t.read32(GPIO_CTRL)
    reg &= ~GPIO_CTRL_DIR7              # output
    reg |= GPIO_CTRL_VAL7               # high for 2.4G
    t.write32(GPIO_CTRL, reg & 0xFFFFFFFF)

    # ---- (9) Kick the new tune (RFCSR7_RF_TUNING = 1) --------------
    rfcsr = rfcsr_read(t, 7)
    rfcsr |= RFCSR7_RF_TUNING
    rfcsr_write(t, 7, rfcsr & 0xFF)

    # ---- (10) Post-RF dispatcher tail (rt2800lib.c:4257+) ----------
    # NB: the kernel's RFCSR30 H20M + RFCSR3 VCOCAL block at
    # rt2800lib.c:4228-4254 is gated on RF chip == RF3070/53xx, so
    # RT3572 (RF3052) skips it entirely. The 2.4-GHz "BBP changes"
    # else-branch DOES apply (line 4298+), then the BBP82/75 block
    # (line 4308+) and tx_pin assembly (line 4356+).

    # BBP noise-floor writes (uses lna_gain from EEPROM).
    nf = (0x37 - (lna_gain & 0xFF)) & 0xFF
    bbp_write(t, 62, nf)
    bbp_write(t, 63, nf)
    bbp_write(t, 64, nf)
    bbp_write(t, 86, 0x00)                # RT3572 != RT6352 → 0

    # BBP82/75 — RT3572 is NOT in the RT5390/RT5392/RT6352 skip-list,
    # so the kernel writes these. has_cap_external_lna_bg requires
    # EEPROM; without it we hit the else branch (0x84 + 0x50).
    bbp_write(t, 82, 0x84)                # not RT3593 → 0x84
    bbp_write(t, 75, 0x50)                # no external LNA flag

    # TX_BAND_CFG — HT20, 2.4 GHz routing.
    reg = t.read32(TX_BAND_CFG_REG)
    reg &= ~TX_BAND_CFG_HT40_MINUS
    reg &= ~TX_BAND_CFG_A                 # channel ≤ 14 → A=0
    reg |= TX_BAND_CFG_BG_BIT             # channel ≤ 14 → BG=1
    t.write32(TX_BAND_CFG_REG, reg & 0xFFFFFFFF)

    # RT3572-only RFCSR8 pre-AGC write.
    rfcsr_write(t, 8, 0)

    # Assemble TX_PIN_CFG. Kernel starts from 0 (not RT6352).
    tx_pin = 0

    # PA enables (per tx_chain_num switch, with case fall-through).
    if tx_chain_num >= 3:
        # PA_PE_A2_EN / PA_PE_G2_EN — not defined in our constants yet
        # (tertiary PAs). AWUS051NH v2 is 2T2R so we never hit 3.
        # If we ever do, the values are bits 0x10 / 0x20 — leaving as
        # a clear gap to surface if it ever needs to be implemented.
        raise NotImplementedError(
            "tx_chain_num >= 3 not supported on RT3572 — 2T2R is the "
            "documented hw config for our supported dongles"
        )
    if tx_chain_num >= 2:
        # Secondary PAs.
        # channel ≤ 14 → PA_PE_G1_EN = 1, PA_PE_A1_EN = 0
        tx_pin |= TX_PIN_CFG_PA_PE_G1_EN
    # Primary PAs (always — case 1).
    if has_cap_bt_coexist:
        tx_pin |= TX_PIN_CFG_PA_PE_G0_EN_BIT
    else:
        tx_pin |= TX_PIN_CFG_PA_PE_G0_EN_BIT     # channel ≤ 14 → 1

    # LNA enables (per rx_chain_num switch).
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
    rfcsr_write(t, 8, 0x80)
    bbp66 = (0x1C + 2 * (lna_gain & 0xFF)) & 0xFF
    bbp_write_with_rx_chain(t, 66, bbp66, rx_chain_num=rx_chain_num)

    # RT3572 settle delay — kernel rt2800lib.c:4465
    # (`usleep_range(1000, 1500)` inside the RT3572 block of
    # rt2800_config_channel, AFTER the AGC init).
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
        "set_channel_3572: ch=%d rf=(%d,%d,%d) tx/rx_chain=(%d,%d) "
        "cal_bw20=0x%02x bbp25/26=0x%02x/0x%02x freq_off=%d",
        channel, rf1, rf2, rf3, tx_chain_num, rx_chain_num,
        cal_result.calibration_bw20, cal_result.bbp25, cal_result.bbp26,
        freq_offset,
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
    tx_chain_num: int = 1,
    rx_chain_num: int = 1,
    has_cap_bt_coexist: bool = False,
    default_power1: int = 0,
    default_power2: int = 0,
) -> None:
    """Tune to ``channel`` on the given silicon.

    All EEPROM-derived kwargs default to "0 / no" so existing call
    sites still work for RT5392 (which ignores everything except
    ``freq_offset`` and ``lna_gain``). RT3572 needs the cal_result
    plus chain counts; pass them in or the call will fail.
    """
    if channel not in _RF_VALS_2G:
        raise ValueError(f"channel {channel} not in 2.4 GHz range (1..14)")
    if silicon_id == RT_RT5392:
        _set_channel_5392(t, channel, freq_offset=freq_offset, lna_gain=lna_gain)
    elif silicon_id == RT_RT3572:
        _set_channel_3572(
            t, channel,
            freq_offset=freq_offset,
            lna_gain=lna_gain,
            cal_result=cal_result,
            txmixer_gain_24g=txmixer_gain_24g,
            tx_chain_num=tx_chain_num,
            rx_chain_num=rx_chain_num,
            has_cap_bt_coexist=has_cap_bt_coexist,
            default_power1=default_power1,
            default_power2=default_power2,
        )
    else:
        raise NotImplementedError(
            f"set_channel for silicon 0x{silicon_id:04x} not yet validated"
        )
