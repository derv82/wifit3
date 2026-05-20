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
import time

from .bbp import bbp4_mac_if_ctrl
from .constants import (
    OPT_14_CSR,
    OPT_14_CSR_BIT0,
    REGISTER_BUSY_COUNT,
    RF_CSR_CFG,
    RF_CSR_CFG_BUSY,
    RF_CSR_CFG_DATA,
    RF_CSR_CFG_REGNUM,
    RF_CSR_CFG_WRITE,
    RFCSR30_RX_VCM,
    RFCSR38_RX_LO1_EN,
    RFCSR39_RX_LO2_EN,
    RT_RT5390,
    RT_RT5392,
)
from .transport import RT2800USBTransport

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


# Public dispatcher — picks the right RF init for the silicon.
def init_rfcsr(t: RT2800USBTransport, silicon_id: int) -> None:
    if silicon_id == RT_RT5392:
        init_rfcsr_5392(t)
    elif silicon_id == RT_RT5390:
        raise NotImplementedError(
            "rt2800_init_rfcsr_5390 not yet ported — user's hw is RT5392, "
            "RT5390 path is a follow-on milestone if a different dongle "
            "shows up that uses the older silicon."
        )
    else:
        raise NotImplementedError(
            f"RF init for silicon 0x{silicon_id:04x} not yet ported"
        )
