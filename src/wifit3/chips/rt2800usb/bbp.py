"""BBP (baseband processor) indirect register access + RT5390/RT5392
init.

The BBP isn't memory-mapped — it's accessed through the BBP_CSR_CFG
register at 0x101C using a busy/owner protocol:

  Write:
    BBP_CSR_CFG = VALUE | (REGNUM << 8) | BUSY | RW_MODE  (READ_CONTROL=0)
  Read:
    BBP_CSR_CFG = (REGNUM << 8) | BUSY | RW_MODE | READ_CONTROL
    (poll BUSY clears, then read VALUE field)

Both ports are 1-byte payloads.

[SRC] rt2800lib.c:53-54 (WAIT_FOR_BBP macro),
      rt2800lib.c:83-140 (rt2800_bbp_write / rt2800_bbp_read),
      rt2800lib.c:6858-6965 (rt2800_init_bbp_53xx).
"""
from __future__ import annotations

import logging
import time

from .constants import (
    BBP4_MAC_IF_CTRL,
    BBP_CSR_CFG,
    BBP_CSR_CFG_BBP_RW_MODE,
    BBP_CSR_CFG_BUSY,
    BBP_CSR_CFG_READ_CONTROL,
    BBP_CSR_CFG_REGNUM,
    BBP_CSR_CFG_VALUE,
    REGISTER_BUSY_COUNT,
    RT_RT5390,
    RT_RT5392,
)
from .transport import RT2800USBTransport

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# BBP busy-wait helper (rt2800_regbusy_read on BBP_CSR_CFG.BUSY).
# ----------------------------------------------------------------------
def _wait_for_bbp(t: RT2800USBTransport) -> int:
    """Poll BBP_CSR_CFG until BUSY clears.  Returns the final register
    value (caller may read VALUE field from it).  Returns 0xFFFFFFFF on
    timeout — matches the kernel "we couldn't get the BBP" pattern."""
    for _ in range(REGISTER_BUSY_COUNT):
        reg = t.read32(BBP_CSR_CFG)
        if not (reg & BBP_CSR_CFG_BUSY):
            return reg
        time.sleep(0.000_05)  # 50µs; kernel uses udelay loop
    logger.warning("BBP_CSR_CFG.BUSY never cleared")
    return 0xFFFFFFFF


def bbp_write(t: RT2800USBTransport, word: int, value: int) -> None:
    """Write a single BBP register.  Mirrors rt2800_bbp_write."""
    reg = _wait_for_bbp(t)
    if reg == 0xFFFFFFFF:
        return                       # kernel just swallows the write
    reg = 0
    reg |= value & BBP_CSR_CFG_VALUE
    reg |= (word << 8) & BBP_CSR_CFG_REGNUM
    reg |= BBP_CSR_CFG_BUSY
    reg |= BBP_CSR_CFG_BBP_RW_MODE
    # READ_CONTROL stays 0 for writes.
    t.write32(BBP_CSR_CFG, reg)


def bbp_read(t: RT2800USBTransport, word: int) -> int:
    """Read a single BBP register.  Mirrors rt2800_bbp_read.  Returns
    0xFF on timeout (kernel convention)."""
    reg = _wait_for_bbp(t)
    if reg == 0xFFFFFFFF:
        return 0xFF
    reg = 0
    reg |= (word << 8) & BBP_CSR_CFG_REGNUM
    reg |= BBP_CSR_CFG_BUSY
    reg |= BBP_CSR_CFG_READ_CONTROL
    reg |= BBP_CSR_CFG_BBP_RW_MODE
    t.write32(BBP_CSR_CFG, reg)

    reg = _wait_for_bbp(t)
    if reg == 0xFFFFFFFF:
        return 0xFF
    return reg & BBP_CSR_CFG_VALUE


# ----------------------------------------------------------------------
# Small BBP helpers — direct ports of rt2800lib.c functions.
# ----------------------------------------------------------------------
def bbp4_mac_if_ctrl(t: RT2800USBTransport) -> None:
    """R-M-W on BBP[4]: set BBP4_MAC_IF_CTRL bit (0x40).
    [SRC] rt2800lib.c:6378-6385"""
    value = bbp_read(t, 4)
    value |= BBP4_MAC_IF_CTRL
    bbp_write(t, 4, value & 0xFF)


def init_freq_calibration(t: RT2800USBTransport) -> None:
    """[SRC] rt2800lib.c:6387-6391."""
    bbp_write(t, 142, 1)
    bbp_write(t, 143, 57)


# ----------------------------------------------------------------------
# rt2800_init_bbp_53xx — for RT5390 (covers RT5370/RT5372) and RT5392.
# [SRC] rt2800lib.c:6858-6965
#
# Deferred for now (need EEPROM bring-up — see
# [[feedback_defer_efuse_on_bring_up]]):
#   * BBP106 RT5390 case (we only handle RT5392 silicon for now)
#     [actually we do handle both — see code]
#   * disable_unused_dac_adc  (reads EEPROM_NIC_CONF0 TXPATH/RXPATH)
#   * Bluetooth coex GPIO_CTRL writes
#   * rev-dependent hw antenna diversity BBP150/151/154 setup
#   * BBP152 RX_DEFAULT_ANT (uses EEPROM-derived ant index)
# ----------------------------------------------------------------------
def init_bbp_53xx(t: RT2800USBTransport, silicon_id: int) -> None:
    """Port of rt2800_init_bbp_53xx, RT5390/RT5392 path.

    ~30 BBP register writes for the baseband bring-up. EEPROM-dependent
    branches are deferred (see module docstring).
    """
    if silicon_id not in (RT_RT5390, RT_RT5392):
        raise ValueError(
            f"init_bbp_53xx called with unsupported silicon 0x{silicon_id:04x} "
            f"(expected RT5390=0x5390 or RT5392=0x5392)"
        )

    bbp4_mac_if_ctrl(t)

    bbp_write(t, 31, 0x08)

    bbp_write(t, 65, 0x2C)
    bbp_write(t, 66, 0x38)

    bbp_write(t, 68, 0x0B)

    bbp_write(t, 69, 0x12)
    bbp_write(t, 73, 0x13)
    bbp_write(t, 75, 0x46)
    bbp_write(t, 76, 0x28)

    bbp_write(t, 77, 0x59)

    bbp_write(t, 70, 0x0A)

    bbp_write(t, 79, 0x13)
    bbp_write(t, 80, 0x05)
    bbp_write(t, 81, 0x33)

    bbp_write(t, 82, 0x62)

    bbp_write(t, 83, 0x7A)

    bbp_write(t, 84, 0x9A)

    bbp_write(t, 86, 0x38)

    if silicon_id == RT_RT5392:
        bbp_write(t, 88, 0x90)

    bbp_write(t, 91, 0x04)

    bbp_write(t, 92, 0x02)

    if silicon_id == RT_RT5392:
        bbp_write(t, 95, 0x9A)
        bbp_write(t, 98, 0x12)

    bbp_write(t, 103, 0xC0)

    bbp_write(t, 104, 0x92)

    bbp_write(t, 105, 0x3C)

    # BBP106 differs per silicon (kernel uses WARN_ON for any other).
    if silicon_id == RT_RT5390:
        bbp_write(t, 106, 0x03)
    else:  # RT5392
        bbp_write(t, 106, 0x12)

    bbp_write(t, 128, 0x12)

    if silicon_id == RT_RT5392:
        bbp_write(t, 134, 0xD0)
        bbp_write(t, 135, 0xF6)

    # Deferred:
    #   disable_unused_dac_adc       (EEPROM NIC_CONF0)
    #   antenna diversity setup      (EEPROM NIC_CONF1)
    #   Bluetooth coex GPIO_CTRL     (EEPROM has_cap_bt_coexist)
    #   BBP150/151/154 hw antenna    (rev-dependent)
    #   BBP152 RX_DEFAULT_ANT        (EEPROM-derived ant index)

    init_freq_calibration(t)
