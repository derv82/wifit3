"""RFCSR (RF chip serial register) indirect access + RT5392 RF init.

RF registers are accessed through RF_CSR_CFG at 0x0500 — same shape as
the BBP protocol but different bit layout (REGNUM in bits[13:8],
WRITE in bit 16, BUSY in bit 17, DATA in bits[7:0]).

[SRC] rt2800lib.c:142-181 (rt2800_rfcsr_write)
      rt2800lib.c:223-280 (rt2800_rfcsr_read)
      rt2800lib.c:7385-7396 (rt2800_rf_init_calibration)
      rt2800lib.c:8394-8460 (rt2800_init_rfcsr_5392)
      rt2800lib.c:7551-7578 (rt2800_normal_mode_setup_5xxx)
"""
from __future__ import annotations

import logging
import os
import time

from dataclasses import dataclass

from .bbp import bbp4_mac_if_ctrl, bbp_read, bbp_write
from .constants import (
    BBP4_BANDWIDTH,
    FREQ_OFFSET_BOUND,
    LDO_CFG0,
    LDO_CFG0_BGSEL,
    LDO_CFG0_LDO_CORE_VLEVEL,
    OPT_14_CSR,
    OPT_14_CSR_BIT0,
    REGISTER_BUSY_COUNT,
    REV_RT5592C,
    RF_CSR_CFG,
    RF_CSR_CFG_BUSY,
    RF_CSR_CFG_DATA,
    RF_CSR_CFG_REGNUM,
    RF_CSR_CFG_WRITE,
    RFCSR6_R2,
    RFCSR17_CODE,
    RFCSR17_TX_LO1_EN,
    RFCSR17_TXMIXER_GAIN,
    RFCSR22_BASEBAND_LOOPBACK,
    RFCSR30_RX_VCM,
    RFCSR31_RX_H20M,
    RFCSR38_RX_LO1_EN,
    RFCSR39_RX_LO2_EN,
    RT_RT3572,
    RT_RT5390,
    RT_RT5392,
    RT_RT5592,
)
from .transport import RT2800USBTransport

# MCU command for freq offset update on USB.  [SRC] rt2800.h
MCU_FREQ_OFFSET = 0x74

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# RF_CSR_CFG busy-wait — analogous to _wait_for_bbp.
# ----------------------------------------------------------------------
def _wait_for_rfcsr(t: RT2800USBTransport) -> int:
    """Poll RF_CSR_CFG until BUSY clears.  Returns the final word, or
    0xFFFFFFFF on timeout (matches kernel's "give up" pattern)."""
    for _ in range(REGISTER_BUSY_COUNT):
        reg = t.read32(RF_CSR_CFG)
        if not (reg & RF_CSR_CFG_BUSY):
            return reg
        time.sleep(0.000_05)
    logger.warning("RF_CSR_CFG.BUSY never cleared")
    return 0xFFFFFFFF


def rfcsr_write(t: RT2800USBTransport, word: int, value: int) -> None:
    """Write a single RF register.  Mirrors rt2800_rfcsr_write (default
    branch — RT6352/MT7620 uses a separate bit layout we don't need)."""
    reg = _wait_for_rfcsr(t)
    if reg == 0xFFFFFFFF:
        return
    reg = 0
    reg |= value & RF_CSR_CFG_DATA
    reg |= (word << 8) & RF_CSR_CFG_REGNUM
    reg |= RF_CSR_CFG_WRITE
    reg |= RF_CSR_CFG_BUSY
    t.write32(RF_CSR_CFG, reg)


def rfcsr_read(t: RT2800USBTransport, word: int) -> int:
    """Read a single RF register.  Returns 0xFF on timeout."""
    reg = _wait_for_rfcsr(t)
    if reg == 0xFFFFFFFF:
        return 0xFF
    reg = 0
    reg |= (word << 8) & RF_CSR_CFG_REGNUM
    # WRITE = 0 for read
    reg |= RF_CSR_CFG_BUSY
    t.write32(RF_CSR_CFG, reg)

    reg = _wait_for_rfcsr(t)
    if reg == 0xFFFFFFFF:
        return 0xFF
    return reg & RF_CSR_CFG_DATA


# ----------------------------------------------------------------------
# rt2800_freq_cal_mode1 (USB branch) — clamp freq_offset to
# FREQ_OFFSET_BOUND and push it to RFCSR17.CODE via the MCU_FREQ_OFFSET
# request. Used both by init_rfcsr_5xxx (after the bulk RFCSR table) and
# by every channel tune on RF53xx / RF55xx silicon.
# [SRC] rt2800lib.c:2447-2480
# ----------------------------------------------------------------------
def freq_cal_mode1_usb(t: RT2800USBTransport, freq_offset: int = 0) -> None:
    """USB version of freq_cal_mode1: clamp + MCU_FREQ_OFFSET.

    For non-USB the kernel walks the freq trim incrementally; on USB the
    MCU does the walk for us via the MCU_FREQ_OFFSET command.
    """
    from .firmware import mcu_request

    code = min(freq_offset & RFCSR17_CODE, FREQ_OFFSET_BOUND)
    rfcsr17 = rfcsr_read(t, 17)
    mcu_request(
        t, MCU_FREQ_OFFSET,
        token=0xFF, arg0=code & 0xFF, arg1=rfcsr17 & 0xFF,
    )


# ----------------------------------------------------------------------
# rt2800_rf_init_calibration — toggle RFCSR.BIT(7) with a 1ms pause.
# [SRC] rt2800lib.c:7385-7396
# ----------------------------------------------------------------------
def rf_init_calibration(t: RT2800USBTransport, rf_reg: int) -> None:
    """Trigger a cal cycle on RFCSR[rf_reg] by setting + clearing bit 7."""
    rfcsr = rfcsr_read(t, rf_reg)
    rfcsr |= 0x80
    rfcsr_write(t, rf_reg, rfcsr)
    time.sleep(0.001)
    rfcsr &= ~0x80
    rfcsr_write(t, rf_reg, rfcsr & 0xFF)


# ----------------------------------------------------------------------
# rt2800_led_open_drain_enable — OPT_14_CSR bit 0 = 1.
# [SRC] rt2800lib.c:7311-7318
# ----------------------------------------------------------------------
def led_open_drain_enable(t: RT2800USBTransport) -> None:
    reg = t.read32(OPT_14_CSR)
    reg |= OPT_14_CSR_BIT0
    t.write32(OPT_14_CSR, reg & 0xFFFFFFFF)


# ----------------------------------------------------------------------
# rt2800_normal_mode_setup_5xxx — post-RF-init tweaks (RX_LO disables,
# BBP4 MAC_IF_CTRL, RFCSR30 RX_VCM=2).  [SRC] rt2800lib.c:7551-7578
#
# The DAC1/ADC1 power-down at the top reads EEPROM_NIC_CONF0 — we
# defer that (per [[feedback_defer_efuse_on_bring_up]]) so we just
# do the RX_LO + bbp4 + RX_VCM tail.
# ----------------------------------------------------------------------
def normal_mode_setup_5xxx(t: RT2800USBTransport) -> None:
    # Deferred: BBP138 RX_ADC1 / TX_DAC1 setup (needs EEPROM_NIC_CONF0).

    # Disable RX_LO1.
    rfcsr = rfcsr_read(t, 38)
    rfcsr &= ~RFCSR38_RX_LO1_EN & 0xFF
    rfcsr_write(t, 38, rfcsr)

    # Disable RX_LO2.
    rfcsr = rfcsr_read(t, 39)
    rfcsr &= ~RFCSR39_RX_LO2_EN & 0xFF
    rfcsr_write(t, 39, rfcsr)

    # Set BBP4 MAC_IF_CTRL (kernel re-asserts this here even though
    # init_bbp_53xx also does it).
    bbp4_mac_if_ctrl(t)

    # RFCSR30 RX_VCM = 2  (bits[4:3] of RFCSR30)
    rfcsr = rfcsr_read(t, 30)
    rfcsr = (rfcsr & ~RFCSR30_RX_VCM) | ((2 << 3) & RFCSR30_RX_VCM)
    rfcsr_write(t, 30, rfcsr & 0xFF)


# ----------------------------------------------------------------------
# rt2800_init_rfcsr_5392 — full RT5392 RF init.
# [SRC] rt2800lib.c:8394-8460
# ----------------------------------------------------------------------
_RT5392_RFCSR_INIT_TABLE = (
    # (rfcsr_index, value)
    (1, 0x17),  (3, 0x88),  (5, 0x10),  (6, 0xe0),
    (7, 0x00),  (10, 0x53), (11, 0x4a), (12, 0x46),
    (13, 0x9f), (14, 0x00), (15, 0x00), (16, 0x00),
    (18, 0x03), (19, 0x4d), (20, 0x00), (21, 0x8d),
    (22, 0x20), (23, 0x0b), (24, 0x44), (25, 0x80),
    (26, 0x82), (27, 0x09), (28, 0x00), (29, 0x10),
    (30, 0x10), (31, 0x80), (32, 0x20), (33, 0xC0),
    (34, 0x07), (35, 0x12), (36, 0x00), (37, 0x08),
    (38, 0x89), (39, 0x1b), (40, 0x0f), (41, 0xbb),
    (42, 0xd5), (43, 0x9b), (44, 0x0e), (45, 0xa2),
    (46, 0x73), (47, 0x0c), (48, 0x10), (49, 0x94),
    (50, 0x94), (51, 0x3a), (52, 0x48), (53, 0x44),
    (54, 0x38), (55, 0x43), (56, 0xa1), (57, 0x00),
    (58, 0x39), (59, 0x07), (60, 0x45), (61, 0x91),
    (62, 0x39), (63, 0x07),
)


def init_rfcsr_5392(t: RT2800USBTransport) -> None:
    """Port of rt2800_init_rfcsr_5392 (rt2800lib.c:8394-8460).

    Runs:
      * rf_init_calibration(RFCSR2)
      * 56-entry RT5392-specific RFCSR table
      * normal_mode_setup_5xxx (RX_LO disables + bbp4 + RX_VCM)
      * led_open_drain_enable
    """
    # Trigger RF calibration on RFCSR2 (the magic "cal kick" reg for 5xxx).
    rf_init_calibration(t, 2)

    # Bulk RFCSR write table.
    for word, value in _RT5392_RFCSR_INIT_TABLE:
        rfcsr_write(t, word, value)

    normal_mode_setup_5xxx(t)

    led_open_drain_enable(t)


# ----------------------------------------------------------------------
# RT3572 / RF3052 RF init.
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class RfFilterCal:
    """Per-bandwidth filter calibration result returned by
    rt2800_rx_filter_calibration.  These values need to be replayed
    into the channel-tune path (RFCSR24/RFCSR31) on every channel
    change, so the driver caches them after init_rfcsr_3572 returns.

    bbp25/bbp26 are also captured by the calibration helper because
    rt2800_config_channel_rf3052 restores them at the start of every
    tune (kernel comment: "Restore BBP 25 & 26 for 2.4 GHz").
    """
    calibration_bw20: int
    calibration_bw40: int
    bbp25: int
    bbp26: int


_RT3572_RFCSR_INIT_TABLE = (
    # (rfcsr_index, value) — [SRC] rt2800lib.c:7907-7937
    (0,  0x70), (1,  0x81), (2,  0xF1), (3,  0x02),
    (4,  0x4C), (5,  0x05), (6,  0x4A), (7,  0xD8),
    (9,  0xC3), (10, 0xF1), (11, 0xB9), (12, 0x70),
    (13, 0x65), (14, 0xA0), (15, 0x53), (16, 0x4C),
    (17, 0x23), (18, 0xAC), (19, 0x93), (20, 0xB3),
    (21, 0xD0), (22, 0x00), (23, 0x3C), (24, 0x16),
    (25, 0x15), (26, 0x85), (27, 0x00), (28, 0x00),
    (29, 0x9B), (30, 0x09), (31, 0x10),
)


def _init_rx_filter(
    t: RT2800USBTransport, *, bw40: bool, filter_target: int
) -> int:
    """Port of rt2800_init_rx_filter (rt2800lib.c:7320-7383).

    Tunes RFCSR24 so the chip's RX filter has the right passband/
    stopband response. Iterates BBP55 readings while bumping RFCSR24,
    stopping when the passband-stopband ratio crosses the target.
    Returns the final RFCSR24 value (which the channel-tune path
    will replay).
    """
    rfcsr24 = 0x27 if bw40 else 0x07
    rfcsr_write(t, 24, rfcsr24)

    bbp = bbp_read(t, 4)
    bbp = (bbp & ~BBP4_BANDWIDTH) | ((2 * int(bw40) << 3) & BBP4_BANDWIDTH)
    bbp_write(t, 4, bbp & 0xFF)

    rfcsr = rfcsr_read(t, 31)
    if bw40:
        rfcsr |= RFCSR31_RX_H20M
    else:
        rfcsr &= ~RFCSR31_RX_H20M & 0xFF
    rfcsr_write(t, 31, rfcsr)

    rfcsr = rfcsr_read(t, 22)
    rfcsr |= RFCSR22_BASEBAND_LOOPBACK
    rfcsr_write(t, 22, rfcsr & 0xFF)

    # Probe passband: write BBP24=0, repeatedly tap BBP25=0x90, read
    # BBP55 until non-zero (kernel: "Set power & frequency of passband
    # test tone").
    #
    # Each BBP55 read needs the test tone settled first. The kernel does
    # msleep(1) then a single read ([SRC] rt2800lib.c:7352). On Windows
    # time.sleep() quantizes to the ~15.6 ms scheduler tick (a sub-tick sleep
    # returns ~0 or a full tick), so a fixed sleep samples the tone mid-settle
    # and skews BBP55 a few counts — enough to tip the (passband - stopband)
    # vs filter_target comparison and break the tune one iteration in. Busy-wait
    # the high-res clock for a real, reliable delay (>= the kernel's 1 ms), then
    # read once, exactly as the kernel does.
    def settle() -> None:
        end = time.perf_counter() + 0.002
        while time.perf_counter() < end:
            pass

    bbp_write(t, 24, 0)
    passband = 0
    passband_samples: list[int] = []
    for i in range(100):
        bbp_write(t, 25, 0x90)
        settle()
        passband = bbp_read(t, 55)
        if i < 5 or i % 20 == 0:
            passband_samples.append(passband)
        if passband:
            break
    logger.debug(
        "_init_rx_filter passband (bw40=%s) samples (first/sparse): %s, final=0x%02x",
        bw40, [f"0x{x:02x}" for x in passband_samples], passband,
    )

    # Probe stopband: BBP24=0x06, walk RFCSR24 up while
    # (passband - stopband) <= filter_target.
    bbp_write(t, 24, 0x06)
    overtuned = 0
    stopband_samples: list[tuple[int, int]] = []  # (iter, stopband)
    for i in range(100):
        bbp_write(t, 25, 0x90)
        settle()
        stopband = bbp_read(t, 55)
        if i < 5 or i % 20 == 0:
            stopband_samples.append((i, stopband))
        if (passband - stopband) <= filter_target:
            rfcsr24 = (rfcsr24 + 1) & 0xFF
            if (passband - stopband) == filter_target:
                overtuned += 1
        else:
            break
        rfcsr_write(t, 24, rfcsr24)
    logger.debug(
        "_init_rx_filter stopband (bw40=%s) samples (iter, val): %s",
        bw40, [(i, f"0x{x:02x}") for i, x in stopband_samples],
    )

    if overtuned:
        rfcsr24 = (rfcsr24 - 1) & 0xFF
    rfcsr_write(t, 24, rfcsr24)
    logger.debug(
        "_init_rx_filter(bw40=%s, tgt=0x%02x): passband=0x%02x "
        "stopband=0x%02x rfcsr24=0x%02x overtuned=%d",
        bw40, filter_target, passband, stopband, rfcsr24, overtuned,
    )
    return rfcsr24


def _rx_filter_calibration_3572(t: RT2800USBTransport) -> RfFilterCal:
    """Port of rt2800_rx_filter_calibration for RT3572 path.

    Two _init_rx_filter passes (bw20 / bw40), then capture BBP25/26
    for replay during channel switching, then restore the chip state
    (BBP24 + RFCSR22 loopback off + BBP4 back to BW20).
    [SRC] rt2800lib.c:7398-7442
    """
    # RT3572 path uses target 0x13 / 0x15 (not RT3070's 0x16 / 0x19).
    bw20 = _init_rx_filter(t, bw40=False, filter_target=0x13)
    bw40 = _init_rx_filter(t, bw40=True, filter_target=0x15)

    # Save BBP25/26 — kernel: "for later use in channel switching (for 3052)"
    bbp25 = bbp_read(t, 25)
    bbp26 = bbp_read(t, 26)

    # Restore chip state.
    bbp_write(t, 24, 0)

    rfcsr = rfcsr_read(t, 22)
    rfcsr &= ~RFCSR22_BASEBAND_LOOPBACK & 0xFF
    rfcsr_write(t, 22, rfcsr)

    bbp = bbp_read(t, 4)
    bbp = bbp & ~BBP4_BANDWIDTH    # BW20
    bbp_write(t, 4, bbp & 0xFF)

    # Degenerate-cal override (RT3572 on erased EFUSE). With no factory RF
    # cal the loopback has no real filter response, so the tune loop rails —
    # it breaks on step 0 (low, 0x07) or runs to the iteration cap (high,
    # 0x6b). The in-tree driver rails HIGH on this hardware: [WIRE]
    # aireplay.pcap marches RFCSR24 0x07->0x6b and ships calibration_bw20=0x6b
    # (config_channel writes both RFCSR24 and RFCSR31 from it). When our 20 MHz
    # tune rails, ship the kernel's wire value rather than our own meaningless
    # rail. bw40 is left as the loop result -- unused at 20 MHz and we have no
    # wire value to match it against.
    if bw20 in (0x07, 0x6b):
        logger.info(
            "RT3572 rx-filter bw20 degenerate (rail 0x%02x) — shipping "
            "kernel wire value 0x6b", bw20,
        )
        bw20 = 0x6b

    # Dev sweep knob: WIFIT3_RT3572_RXCAL=0xNN forces the shipped bw20 cal
    # (= RFCSR24/31 TX_AGC_FC) to a fixed value, so the degenerate rail can be
    # characterised empirically across the 0x07..0x6b range without a rebuild.
    # Unset -> normal (kernel-wire) behaviour.
    _sweep = os.environ.get("WIFIT3_RT3572_RXCAL")
    if _sweep:
        try:
            bw20 = int(_sweep, 0) & 0xFF
            logger.info(
                "RT3572 rx-filter bw20 forced via WIFIT3_RT3572_RXCAL → 0x%02x",
                bw20,
            )
        except ValueError:
            logger.warning(
                "WIFIT3_RT3572_RXCAL=%r is not an int — ignoring", _sweep
            )

    return RfFilterCal(
        calibration_bw20=bw20,
        calibration_bw40=bw40,
        bbp25=bbp25,
        bbp26=bbp26,
    )


def _normal_mode_setup_3xxx(
    t: RT2800USBTransport, *, txmixer_gain_24g: int = 0
) -> None:
    """Port of rt2800_normal_mode_setup_3xxx for the RT3572 path.

    [SRC] rt2800lib.c:7444-7513

    For RT3572 silicon (not RT3070/3071/3090/3390), the kernel's
    only mandatory write is RFCSR17_TX_LO1_EN = 0. The optional
    RFCSR17_TXMIXER_GAIN write fires only if txmixer_gain_24g >= 2
    (min_gain for non-RT3070 chips); we default to 0 (EEPROM not
    yet wired), which skips it.

    The kernel's RT3071/3090/3390 trailing branch (RFCSR1/15/20/21
    RX_LO tweaks) doesn't apply to RT3572 — RT3572 falls off the
    end of the `else if` chain.
    """
    rfcsr = rfcsr_read(t, 17)
    rfcsr &= ~RFCSR17_TX_LO1_EN & 0xFF
    # RFCSR17_R conditional: RT3572 doesn't match the kernel's
    # rt2x00_rt_rev_lt branches (it's not RT3070/3071/3090/3390),
    # so no R-bit set here.
    min_gain = 2  # RT3572 == not RT3070, so min_gain == 2
    if txmixer_gain_24g >= min_gain:
        rfcsr = (rfcsr & ~RFCSR17_TXMIXER_GAIN) | (
            txmixer_gain_24g & RFCSR17_TXMIXER_GAIN
        )
    rfcsr_write(t, 17, rfcsr & 0xFF)


def _ldo_cfg0_dance(t: RT2800USBTransport) -> None:
    """LDO_CFG0 two-step write from rt2800_init_rfcsr_3572.

    Mirrors lines 7943-7951:
      VLEVEL=3, BGSEL=1   → write
      msleep(1)
      VLEVEL=0, BGSEL=1   → write
    """
    reg = t.read32(LDO_CFG0)
    reg = (reg & ~LDO_CFG0_LDO_CORE_VLEVEL) | ((3 << 26) & LDO_CFG0_LDO_CORE_VLEVEL)
    reg = (reg & ~LDO_CFG0_BGSEL) | ((1 << 24) & LDO_CFG0_BGSEL)
    t.write32(LDO_CFG0, reg & 0xFFFFFFFF)
    # Real settle for the RF-core LDO. The kernel's msleep(1) lets VLEVEL=3 take
    # effect before VLEVEL=0; a Windows time.sleep quantizes to the ~15.6 ms
    # scheduler tick (returns ~0), leaving the regulator mid-transient when the
    # rx-filter cal runs next — which rails the loopback passband near 0x80 and
    # breaks the tune at its 0x07 start. Busy-wait the high-res clock for a real
    # >= 1 ms delay. [SRC] rt2800lib.c:7947.
    _end = time.perf_counter() + 0.002
    while time.perf_counter() < _end:
        pass
    reg = t.read32(LDO_CFG0)
    reg = (reg & ~LDO_CFG0_LDO_CORE_VLEVEL) | ((0 << 26) & LDO_CFG0_LDO_CORE_VLEVEL)
    reg = (reg & ~LDO_CFG0_BGSEL) | ((1 << 24) & LDO_CFG0_BGSEL)
    t.write32(LDO_CFG0, reg & 0xFFFFFFFF)


def init_rfcsr_3572(
    t: RT2800USBTransport, *, txmixer_gain_24g: int = 0
) -> RfFilterCal:
    """Port of rt2800_init_rfcsr_3572 (rt2800lib.c:7900-7956).

    Runs:
      * rf_init_calibration(RFCSR30)  (RT3572 cal kick — not RFCSR2)
      * 30-entry RFCSR init table
      * R-M-W RFCSR6 R2=1
      * LDO_CFG0 dance (BGSEL + LDO_CORE_VLEVEL)
      * rx_filter_calibration (returns calibration_bw20/bw40 + bbp25/26)
      * led_open_drain_enable
      * normal_mode_setup_3xxx

    Returns the RX filter calibration values, which the driver caches
    for the per-channel set_channel calls.
    """
    rf_init_calibration(t, 30)

    for word, value in _RT3572_RFCSR_INIT_TABLE:
        rfcsr_write(t, word, value)

    # R-M-W RFCSR6 R2=1
    rfcsr = rfcsr_read(t, 6)
    rfcsr |= RFCSR6_R2
    rfcsr_write(t, 6, rfcsr & 0xFF)

    _ldo_cfg0_dance(t)

    cal = _rx_filter_calibration_3572(t)
    led_open_drain_enable(t)
    _normal_mode_setup_3xxx(t, txmixer_gain_24g=txmixer_gain_24g)
    return cal


# ----------------------------------------------------------------------
# rt2800_init_rfcsr_5592 — RF5592 (RT5572) RF chain init.
# [SRC] rt2800lib.c:8462-8503
#
# Shape mirrors init_rfcsr_5392 but with the 5592-specific 21-write
# table + RFCSR2=0x80 cal kick + freq_cal_mode1 + rev-gated extras.
# normal_mode_setup_5xxx and led_open_drain_enable are reused as-is
# (they're already silicon-agnostic helpers).
#
# Rev gating:
#   * chip_rev >= REV_RT5592C → BBP103 = 0xc0    (DC filter enable)
#   * chip_rev <  REV_RT5592C → RFCSR27 = 0x03
# Exactly one fires (REV_RT5592C is the boundary). Both are safe no-ops
# if we pass chip_rev=0 — we'd skip the BBP103 write (matching the
# "rev unknown, don't touch" stance from init_bbp_5592) and apply the
# RFCSR27=0x03 write. Hw test will pin down the rev and we can refine.
# ----------------------------------------------------------------------
_RT5592_RFCSR_INIT_TABLE = (
    # [SRC] rt2800lib.c:8466-8486
    (1, 0x3F),  (3, 0x08),  (5, 0x10),  (6, 0xE4),
    (7, 0x00),  (14, 0x00), (15, 0x00), (16, 0x00),
    (18, 0x03), (19, 0x4D), (20, 0x10), (21, 0x8D),
    (26, 0x82), (28, 0x00), (29, 0x10), (33, 0xC0),
    (34, 0x07), (35, 0x12), (47, 0x0C), (53, 0x22),
    (63, 0x07),
)


def init_rfcsr_5592(
    t: RT2800USBTransport,
    *,
    freq_offset: int = 0,
    chip_rev: int = 0,
) -> None:
    """Port of rt2800_init_rfcsr_5592 (rt2800lib.c:8462-8503).

    Runs:
      * rf_init_calibration(RFCSR30)
      * 21-entry RT5592 RFCSR table
      * RFCSR2 = 0x80 (cal kick) + 1ms sleep
      * freq_cal_mode1_usb(freq_offset)
      * REV_RT5592C+: BBP103 = 0xc0  (DC filter enable)
      * normal_mode_setup_5xxx
      * < REV_RT5592C: RFCSR27 = 0x03
      * led_open_drain_enable
    """
    rf_init_calibration(t, 30)

    for word, value in _RT5592_RFCSR_INIT_TABLE:
        rfcsr_write(t, word, value)

    rfcsr_write(t, 2, 0x80)
    time.sleep(0.001)

    freq_cal_mode1_usb(t, freq_offset=freq_offset)

    if chip_rev >= REV_RT5592C:
        bbp_write(t, 103, 0xC0)

    normal_mode_setup_5xxx(t)

    if chip_rev < REV_RT5592C:
        rfcsr_write(t, 27, 0x03)

    led_open_drain_enable(t)


# Public dispatcher — picks the right RF init for the silicon.
def init_rfcsr(
    t: RT2800USBTransport,
    silicon_id: int,
    *,
    txmixer_gain_24g: int = 0,
    freq_offset: int = 0,
    chip_rev: int = 0,
):
    """Initialise the RF chain for the given silicon.

    Returns RfFilterCal for chips that produce one (RT3572 via
    _rx_filter_calibration); None for chips that don't need
    runtime-captured calibration values (RT5390/RT5392/RT5592).
    """
    if silicon_id == RT_RT5392:
        init_rfcsr_5392(t)
        return None
    if silicon_id == RT_RT3572:
        return init_rfcsr_3572(t, txmixer_gain_24g=txmixer_gain_24g)
    if silicon_id == RT_RT5592:
        init_rfcsr_5592(t, freq_offset=freq_offset, chip_rev=chip_rev)
        return None
    if silicon_id == RT_RT5390:
        raise NotImplementedError(
            "rt2800_init_rfcsr_5390 not yet ported — user's hw is RT5392, "
            "RT5390 path is a follow-on milestone if a different dongle "
            "shows up that uses the older silicon."
        )
    raise NotImplementedError(
        f"RF init for silicon 0x{silicon_id:04x} not yet ported"
    )
