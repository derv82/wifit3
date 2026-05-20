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
    BBP27_RX_CHAIN_SEL,
    BBP_CSR_CFG,
    BBP_CSR_CFG_BBP_RW_MODE,
    BBP_CSR_CFG_BUSY,
    BBP_CSR_CFG_READ_CONTROL,
    BBP_CSR_CFG_REGNUM,
    BBP_CSR_CFG_VALUE,
    H2M_BBP_AGENT,
    H2M_INT_SRC,
    H2M_MAILBOX_CSR,
    MCU_BOOT_SIGNAL,
    REGISTER_BUSY_COUNT,
    RT_RT3572,
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
# rt2800_disable_unused_dac_adc — power-saving tweak that's also a hard
# gate for RX on 1T1R silicon (without it, ADC1 is held in powerdown and
# bulk-IN goes silent). Kernel reads EEPROM_NIC_CONF0 for the TX/RX
# path counts; we take them as args so the caller can pass either real
# EEPROM values or the hw-pinned defaults for chips with a fixed config.
#
# [SRC] rt2800lib.c:6434-6446
# ----------------------------------------------------------------------
def disable_unused_dac_adc(
    t: RT2800USBTransport, *, txpath: int, rxpath: int
) -> None:
    value = bbp_read(t, 138)
    if txpath == 1:
        value |= 0x20       # BBP138_TX_DAC1 — power DOWN unused DAC1
    if rxpath == 1:
        value &= ~0x02      # BBP138_RX_ADC1 — power UP ADC1 (clear = active)
    bbp_write(t, 138, value & 0xFF)


# ----------------------------------------------------------------------
# rt2800_bbp_write_with_rx_chain — fan-out a BBP write across each
# active RX path. The kernel writes BBP27.RX_CHAIN_SEL to switch which
# chain's BBP register is exposed, then writes the target word. Used
# by config_channel_rf3052 for the AGC init (BBP66).
#
# [SRC] rt2800lib.c:4011-4024
# ----------------------------------------------------------------------
def bbp_write_with_rx_chain(
    t: RT2800USBTransport, word: int, value: int, *, rx_chain_num: int
) -> None:
    for chain in range(rx_chain_num):
        reg = bbp_read(t, 27)
        reg = (reg & ~BBP27_RX_CHAIN_SEL) | ((chain << 5) & BBP27_RX_CHAIN_SEL)
        bbp_write(t, 27, reg & 0xFF)
        bbp_write(t, word, value & 0xFF)


# ----------------------------------------------------------------------
# wait_bbp_rf_ready + wait_bbp_ready — kernel preludes that MUST run
# between init_registers and init_bbp/init_rfcsr.
#
# [SRC] rt2800lib.c:2225-2241 (wait_bbp_rf_ready)
#       rt2800lib.c:2243-2266 (wait_bbp_ready)
#       rt2800lib.c:10797-10827 (rt2800_enable_radio orchestration)
# ----------------------------------------------------------------------
MAC_STATUS_CFG = 0x1200
MAC_STATUS_CFG_BBP_RF_BUSY = 0x00000003   # bits 0+1


def wait_bbp_rf_ready(t: RT2800USBTransport) -> bool:
    """Poll MAC_STATUS_CFG until BBP_RF_BUSY (bits 0-1) clears.
    Returns True on success."""
    for _ in range(REGISTER_BUSY_COUNT):
        reg = t.read32(MAC_STATUS_CFG)
        if not (reg & MAC_STATUS_CFG_BBP_RF_BUSY):
            return True
        time.sleep(0.000_05)
    logger.warning("MAC_STATUS_CFG.BBP_RF_BUSY never cleared")
    return False


def wait_bbp_ready(t: RT2800USBTransport) -> bool:
    """Reactivate BBP after FW load, then poll BBP[0] until it has a
    real value (not 0x00 or 0xff).  Mirrors the kernel comment
    "BBP was enabled after firmware was loaded, but we need to
    reactivate it now."
    """
    t.write32(H2M_BBP_AGENT, 0)
    t.write32(H2M_MAILBOX_CSR, 0)
    time.sleep(0.001)
    for _ in range(REGISTER_BUSY_COUNT):
        v = bbp_read(t, 0)
        if v not in (0x00, 0xFF):
            return True
        time.sleep(0.000_05)
    logger.warning("BBP[0] never returned a valid value")
    return False


def prepare_bbp(t: RT2800USBTransport) -> None:
    """Bring the BBP up between init_registers and init_bbp.

    Order from rt2800_enable_radio (rt2800lib.c:10797-10827):
      1. wait_bbp_rf_ready (MAC_STATUS_CFG.BBP_RF_BUSY clears)
      2. H2M_BBP_AGENT = 0, H2M_MAILBOX_CSR = 0, H2M_INT_SRC = 0
      3. mcu_request(MCU_BOOT_SIGNAL, 0, 0, 0)
      4. msleep(1)
      5. wait_bbp_ready (re-arm H2M + poll BBP[0])

    Without this prelude, subsequent init_bbp / init_rfcsr writes appear
    to succeed (BBP_CSR_CFG protocol completes) but the BBP→RF chain
    never activates and bulk-IN stays silent.
    """
    from .firmware import mcu_request
    if not wait_bbp_rf_ready(t):
        raise IOError("BBP/RF still busy — chip wedged")
    t.write32(H2M_BBP_AGENT, 0)
    t.write32(H2M_MAILBOX_CSR, 0)
    t.write32(H2M_INT_SRC, 0)
    mcu_request(t, MCU_BOOT_SIGNAL, token=0, arg0=0, arg1=0)
    time.sleep(0.001)
    if not wait_bbp_ready(t):
        raise IOError("BBP[0] never came up — MCU_BOOT_SIGNAL may have failed")


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

    # Deferred (need EEPROM):
    #   disable_unused_dac_adc       (EEPROM NIC_CONF0)
    #   antenna diversity setup      (EEPROM NIC_CONF1)
    #   Bluetooth coex GPIO_CTRL     (EEPROM has_cap_bt_coexist)
    #   BBP150/151/154 hw antenna    (rev-dependent)

    # BBP152 RX_DEFAULT_ANT — actually selects which antenna feeds the
    # RX path. Kernel reads EEPROM NIC_CONF1 for the `ant` value:
    #   ant = 0 → BBP152.RX_DEFAULT_ANT = 1 (bit 7)
    #   ant = 1 → BBP152.RX_DEFAULT_ANT = 0
    # Without EEPROM `ant` defaults to 0 → set bit 7. R-M-W preserves
    # other bits.
    value = bbp_read(t, 152)
    value |= 0x80
    bbp_write(t, 152, value & 0xFF)

    # rt2800_disable_unused_dac_adc — without this write the chip's
    # default ADC1 power-down state silently blackholes RX on 1T1R
    # silicon. RT5390/RT5392 are always 1T1R so we pin both args to 1.
    disable_unused_dac_adc(t, txpath=1, rxpath=1)

    init_freq_calibration(t)


# ----------------------------------------------------------------------
# rt2800_init_bbp_3572 — ~18 BBP writes for the RT3572 baseband.
# Smaller than init_bbp_53xx; doesn't depend on silicon revision.
# Kernel ends with disable_unused_dac_adc which we call with EEPROM-
# derived path counts (AWUS051NH v2 is 2T2R so on that hw the helper
# is effectively a no-op — the kernel-correct behaviour).
#
# [SRC] rt2800lib.c:6764-6799
# ----------------------------------------------------------------------
def init_bbp_3572(
    t: RT2800USBTransport, *, txpath: int = 2, rxpath: int = 2
) -> None:
    bbp_write(t, 31, 0x08)

    bbp_write(t, 65, 0x2c)
    bbp_write(t, 66, 0x38)

    bbp_write(t, 69, 0x12)
    bbp_write(t, 73, 0x10)

    bbp_write(t, 70, 0x0a)

    bbp_write(t, 79, 0x13)
    bbp_write(t, 80, 0x05)
    bbp_write(t, 81, 0x33)

    bbp_write(t, 82, 0x62)

    bbp_write(t, 83, 0x6a)

    bbp_write(t, 84, 0x99)

    bbp_write(t, 86, 0x00)

    bbp_write(t, 91, 0x04)

    bbp_write(t, 92, 0x00)

    bbp_write(t, 103, 0xC0)

    bbp_write(t, 105, 0x05)

    bbp_write(t, 106, 0x35)

    disable_unused_dac_adc(t, txpath=txpath, rxpath=rxpath)


# ----------------------------------------------------------------------
# Public BBP-init dispatcher. Picks the right per-silicon init.
# ----------------------------------------------------------------------
def init_bbp(
    t: RT2800USBTransport,
    silicon_id: int,
    *,
    txpath: int = 1,
    rxpath: int = 1,
) -> None:
    if silicon_id == RT_RT3572:
        init_bbp_3572(t, txpath=txpath, rxpath=rxpath)
    elif silicon_id in (RT_RT5390, RT_RT5392):
        init_bbp_53xx(t, silicon_id)
    else:
        raise NotImplementedError(
            f"BBP init for silicon 0x{silicon_id:04x} not yet ported"
        )
