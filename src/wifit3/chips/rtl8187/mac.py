"""RTL8187L / RTL8187B MAC-layer bring-up + warm/cold helpers.

Mirrors ``rtl8187_{set_anaparam,cmd_reset,init_hw,start,stop}`` from
``driver_sources/rtl818x-source-v6.18/rtl8187/dev.c``.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .constants import (
    CW_CONF_PERPACKET_RETRY,
    MSR_ENEDCA,
    REG_ACM_CONTROL,
    REG_ANAPARAM3A,
    REG_HSSI_PARA,
    REG_INT_MIG,
    REG_MSR,
    REG_TID_AC_MAP,
    RETRY_COUNT,
    RTL8187B_RTL8225_ANAPARAM2_OFF,
    RTL8187B_RTL8225_ANAPARAM2_ON,
    RTL8187B_RTL8225_ANAPARAM3_OFF,
    RTL8187B_RTL8225_ANAPARAM3_ON,
    RTL8187B_RTL8225_ANAPARAM_OFF,
    RTL8187B_RTL8225_ANAPARAM_ON,
    TX_AGC_CTL_FEEDBACK_ANT,
    TX_AGC_CTL_PERPACKET_ANTSEL,
    TX_AGC_CTL_PERPACKET_GAIN,
    TX_CONF_DISREQQSIZE,
    TX_CONF_HW_SEQNUM,
    ANAPARAM_OFF,
    ANAPARAM_ON,
    ANAPARAM2_OFF,
    ANAPARAM2_ON,
    CMD_RESET,
    CMD_RX_ENABLE,
    CMD_TX_ENABLE,
    CONFIG3_ANAPARAM_WRITE,
    CONFIG4_VCOOFF,
    EEPROM_CMD_CONFIG,
    EEPROM_CMD_LOAD,
    EEPROM_CMD_NORMAL,
    FIF_ALLMULTI,
    FIF_CONTROL,
    FIF_FCSFAIL,
    FIF_OTHER_BSS,
    HWVER_CHIP_NAMES,
    HWVER_DEFAULT_NAME,
    HwRev,
    REG_ANAPARAM,
    REG_ANAPARAM2,
    REG_BRSR,
    REG_CMD,
    REG_CONFIG1,
    REG_CONFIG3,
    REG_CONFIG4,
    REG_CW_CONF,
    REG_EEPROM_CMD,
    REG_GP_ENABLE,
    REG_GPIO0,
    REG_INT_MASK,
    REG_INT_TIMEOUT,
    REG_MAGIC_FE18,
    REG_MAGIC_FE53,
    REG_MAGIC_FFE1,
    REG_MAGIC_FFF4,
    REG_MAGIC_FFFF,
    REG_MAR,
    REG_PGSELECT,
    REG_RATE_FALLBACK,
    REG_RESP_RATE,
    REG_RF_PARA,
    REG_RF_TIMING,
    REG_RFPINSENABLE,
    REG_RFPINSOUTPUT,
    REG_RFPINSSELECT,
    REG_RX_CONF,
    REG_TALLY_SEL,
    REG_TX_AGC_CTL,
    REG_TX_CONF,
    REG_WPA_CONF,
    RX_CONF_BROADCAST,
    RX_CONF_BSSID,
    RX_CONF_CTRL,
    RX_CONF_DATA,
    RX_CONF_FCS,
    RX_CONF_MGMT,
    RX_CONF_MONITOR,
    RX_CONF_NICMAC,
    RX_CONF_ONLYERLPKT,
    RX_CONF_RX_AUTORESETPHY,
    RTL818X_R8187B_B,
    RTL818X_R8187B_D,
    RTL818X_R8187B_E,
    TX_CONF_CW_MIN,
    TX_CONF_HWVER_MASK,
    TX_CONF_NO_ICV,
    TX_CONF_R8187vD_B,
)
from .transport import RTL8187Transport

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Chip-variant probe (M1)
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ChipVariant:
    hwver_raw: Optional[int]    # TX_CONF HWVER bits; None on a DEVICE_RTL8187B id (never read)
    name: str                   # chip_name
    is_rtl8187b: bool
    hw_rev: Optional[HwRev]     # priv->hw_rev; None on 8187L silicon


def detect_chip_variant(t: RTL8187Transport) -> ChipVariant:
    """Read TX_CONF and decode HWVER bits to identify the chip.

    Mirrors the L-branch switch in rtl8187_probe (dev.c:1539-1556).
    """
    tx_conf = t.read32(REG_TX_CONF)
    hwver = tx_conf & TX_CONF_HWVER_MASK
    name = HWVER_CHIP_NAMES.get(hwver, HWVER_DEFAULT_NAME)
    if hwver == TX_CONF_R8187vD_B:      # early 8187B behind the 0x8187 PID (dev.c:1544-1550)
        return ChipVariant(hwver_raw=hwver, name=name, is_rtl8187b=True, hw_rev=HwRev.RTL8187BvB)
    return ChipVariant(hwver_raw=hwver, name=name, is_rtl8187b=False, hw_rev=None)


def detect_rtl8187b_hw_rev(t: RTL8187Transport) -> ChipVariant:
    """The B-branch switch on 0xFFE1 in rtl8187_probe (dev.c:1567-1583)."""
    reg = t.read8(REG_MAGIC_FFE1)
    if reg == RTL818X_R8187B_B:
        name, hw_rev = "RTL8187BvB", HwRev.RTL8187BvB
    elif reg == RTL818X_R8187B_D:
        name, hw_rev = "RTL8187BvD", HwRev.RTL8187BvD
    elif reg == RTL818X_R8187B_E:
        name, hw_rev = "RTL8187BvE", HwRev.RTL8187BvE
    else:
        name, hw_rev = "RTL8187BvB (default)", HwRev.RTL8187BvB
    return ChipVariant(hwver_raw=None, name=name, is_rtl8187b=True, hw_rev=hw_rev)


def is_chip_warm(t: RTL8187Transport) -> bool:
    """True iff a prior session left CMD with both TX_ENABLE and RX_ENABLE
    set."""
    try:
        cmd = t.read8(REG_CMD)
    except Exception as e:
        logger.debug("REG_CMD read failed during warm probe: %s", e)
        return False
    return bool(cmd & CMD_TX_ENABLE) and bool(cmd & CMD_RX_ENABLE)


# ----------------------------------------------------------------------
# Bring-up: set_anaparam, cmd_reset, init_hw, start  (M2a)
# ----------------------------------------------------------------------
# priv->rf->init; rtl8225.build_rf_init returns the one for the probed RF.
RFInit = Callable[[RTL8187Transport], None]


def set_anaparam(t: RTL8187Transport, rfon: bool, is_rtl8187b: bool) -> None:
    """Port of rtl8187_set_anaparam (dev.c:570-608): the ANAPARAM writes, gated by
    CONFIG3_ANAPARAM_WRITE inside an EEPROM_CMD CONFIG window."""
    anaparam3 = 0
    if not is_rtl8187b:
        if rfon:
            anaparam = ANAPARAM_ON
            anaparam2 = ANAPARAM2_ON
        else:
            anaparam = ANAPARAM_OFF
            anaparam2 = ANAPARAM2_OFF
    else:
        if rfon:
            anaparam = RTL8187B_RTL8225_ANAPARAM_ON
            anaparam2 = RTL8187B_RTL8225_ANAPARAM2_ON
            anaparam3 = RTL8187B_RTL8225_ANAPARAM3_ON
        else:
            anaparam = RTL8187B_RTL8225_ANAPARAM_OFF
            anaparam2 = RTL8187B_RTL8225_ANAPARAM2_OFF
            anaparam3 = RTL8187B_RTL8225_ANAPARAM3_OFF

    t.write8(REG_EEPROM_CMD, EEPROM_CMD_CONFIG)
    reg = t.read8(REG_CONFIG3)
    reg |= CONFIG3_ANAPARAM_WRITE
    t.write8(REG_CONFIG3, reg)
    t.write32(REG_ANAPARAM, anaparam)
    t.write32(REG_ANAPARAM2, anaparam2)
    if is_rtl8187b:
        t.write8(REG_ANAPARAM3A, anaparam3)
    reg &= ~CONFIG3_ANAPARAM_WRITE
    t.write8(REG_CONFIG3, reg & 0xFF)
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_NORMAL)


def cmd_reset(t: RTL8187Transport) -> None:
    """Issue a soft reset to the MAC + reload registers from EEPROM.

    Mirrors rtl8187_cmd_reset (dev.c:610-651). Raises IOError on the
    1.5-decisecond timeouts the kernel uses.
    """
    # Preserve bit 1 of CMD across the reset (kernel does `reg &= (1 << 1)`).
    reg = t.read8(REG_CMD) & (1 << 1)
    reg |= CMD_RESET
    t.write8(REG_CMD, reg)

    # Spin up to 10 × 2ms waiting for CMD_RESET to clear (HW self-clears).
    for _ in range(10):
        time.sleep(0.002)
        if not (t.read8(REG_CMD) & CMD_RESET):
            break
    else:
        raise IOError("rtl8187 cmd_reset: CMD_RESET never cleared (timeout)")

    # Trigger EEPROM auto-load → wait for EEPROM_CMD_CONFIG bit to clear.
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_LOAD)
    for _ in range(10):
        time.sleep(0.004)
        if not (t.read8(REG_EEPROM_CMD) & EEPROM_CMD_CONFIG):
            break
    else:
        raise IOError("rtl8187 cmd_reset: EEPROM_CMD_CONFIG never cleared (timeout)")


def init_hw(t: RTL8187Transport, rf_init: RFInit) -> None:
    """Port of rtl8187_init_hw (dev.c:653-737) — L-branch.

    Steps in order (each comment is the corresponding kernel line range):
      570-608  set_anaparam(rfon=True)        [pre-reset, settles analog]
      662      INT_MASK = 0
      663-668  Magic 0xFE18 toggle sequence (10/11/00) + 200ms
      670-672  cmd_reset
      674      set_anaparam(rfon=True)        [post-reset, re-arm]
      677-682  RFPinsSelect=0, GPIO0=0 → then RFPinsSelect=0x400, GPIO0=1, GP_ENABLE=0
      684      EEPROM_CMD = CONFIG          [open analog window]
      686-690  0xFFF4=0xFFFF + CONFIG1 = (cur & 0x3F) | 0x80
      692      EEPROM_CMD = NORMAL
      694-700  INT_TIMEOUT=0, WPA_CONF=0, RATE_FALLBACK=0, RESP_RATE=(8<<4|0), BRSR=0x01F3
      702-712  host_usb_init: RFPinsSelect=0, GPIO0=0, 0xFE53 |= 0x80,
               RFPinsSelect=0x400, GPIO0=0x20, GP_ENABLE=0,
               RFPinsOutput=0x80, RFPinsSelect=0x80, RFPinsEnable=0x80
      713      msleep(100)
      715-723  RF_TIMING=0x000A8008, BRSR=0xFFFF, RF_PARA=0x00100044,
               CONFIG3=0x44 (bracketed by EEPROM CONFIG/NORMAL),
               RFPinsEnable=0x1FF7
      724      msleep(100)
      726      priv->rf->init(dev)
      728-734  BRSR=0x01F3 + PGSELECT page-1 magic regs (0xFFFE=0x10,
               TALLY_SEL=0x80, 0xFFFF=0x60) bracketed by PGSELECT reg
    """
    # ---- pre-reset analog ON --------------------------------------------------
    set_anaparam(t, rfon=True, is_rtl8187b=False)
    t.write16(REG_INT_MASK, 0)

    time.sleep(0.200)

    # Magic 0xFE18 reset sequence — kernel does idx=0 implicitly. These
    # are non-CSR registers (below 0xFF00) addressed directly.
    t.write8(REG_MAGIC_FE18, 0x10)
    t.write8(REG_MAGIC_FE18, 0x11)
    t.write8(REG_MAGIC_FE18, 0x00)

    time.sleep(0.200)

    cmd_reset(t)

    # ---- post-reset analog ON, re-arm ----------------------------------------
    set_anaparam(t, rfon=True, is_rtl8187b=False)

    # setup card (RF pin defaults)
    t.write16(REG_RFPINSSELECT, 0)
    t.write8(REG_GPIO0, 0)

    t.write16(REG_RFPINSSELECT, 4 << 8)  # 0x0400
    t.write8(REG_GPIO0, 1)
    t.write8(REG_GP_ENABLE, 0)

    # EEPROM CONFIG window open → CONFIG1 = (cur & 0x3F) | 0x80
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_CONFIG)
    t.write16(REG_MAGIC_FFF4, 0xFFFF)
    reg = t.read8(REG_CONFIG1)
    reg &= 0x3F
    reg |= 0x80
    t.write8(REG_CONFIG1, reg)
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_NORMAL)

    # Misc clears
    t.write32(REG_INT_TIMEOUT, 0)
    t.write8(REG_WPA_CONF, 0)
    t.write8(REG_RATE_FALLBACK, 0)

    # TODO (kernel comment): set RESP_RATE and BRSR properly
    t.write8(REG_RESP_RATE, (8 << 4) | 0)
    t.write16(REG_BRSR, 0x01F3)

    # ---- host_usb_init -------------------------------------------------------
    t.write16(REG_RFPINSSELECT, 0)
    t.write8(REG_GPIO0, 0)
    reg = t.read8(REG_MAGIC_FE53)
    t.write8(REG_MAGIC_FE53, reg | (1 << 7))
    t.write16(REG_RFPINSSELECT, 4 << 8)  # 0x0400
    t.write8(REG_GPIO0, 0x20)
    t.write8(REG_GP_ENABLE, 0)
    t.write16(REG_RFPINSOUTPUT, 0x80)
    t.write16(REG_RFPINSSELECT, 0x80)
    t.write16(REG_RFPINSENABLE, 0x80)

    time.sleep(0.100)

    t.write32(REG_RF_TIMING, 0x000A8008)
    t.write16(REG_BRSR, 0xFFFF)
    t.write32(REG_RF_PARA, 0x00100044)
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_CONFIG)
    t.write8(REG_CONFIG3, 0x44)
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_NORMAL)
    t.write16(REG_RFPINSENABLE, 0x1FF7)

    time.sleep(0.100)

    # ---- RF init -------------------------------------------------------------
    rf_init(t)

    # ---- post-RF init: BRSR + page-1 magic regs ------------------------------
    t.write16(REG_BRSR, 0x01F3)
    reg = t.read8(REG_PGSELECT) & ~1
    t.write8(REG_PGSELECT, reg | 1)
    # 0xFFFE = 0x10 (16-bit write per kernel)
    t.write16(0xFFFE, 0x10)
    t.write8(REG_TALLY_SEL, 0x80)
    t.write8(REG_MAGIC_FFFF, 0x60)
    t.write8(REG_PGSELECT, reg & 0xFF)


# [SRC] dev.c:739-767  {addr | 0xFF00, value, idx}
rtl8187b_reg_table = (
    (0xF0, 0x32, 0), (0xF1, 0x32, 0), (0xF2, 0x00, 0), (0xF3, 0x00, 0),
    (0xF4, 0x32, 0), (0xF5, 0x43, 0), (0xF6, 0x00, 0), (0xF7, 0x00, 0),
    (0xF8, 0x46, 0), (0xF9, 0xA4, 0), (0xFA, 0x00, 0), (0xFB, 0x00, 0),
    (0xFC, 0x96, 0), (0xFD, 0xA4, 0), (0xFE, 0x00, 0), (0xFF, 0x00, 0),

    (0x58, 0x4B, 1), (0x59, 0x00, 1), (0x5A, 0x4B, 1), (0x5B, 0x00, 1),
    (0x60, 0x4B, 1), (0x61, 0x09, 1), (0x62, 0x4B, 1), (0x63, 0x09, 1),
    (0xCE, 0x0F, 1), (0xCF, 0x00, 1), (0xF0, 0x4E, 1), (0xF1, 0x01, 1),
    (0xF2, 0x02, 1), (0xF3, 0x03, 1), (0xF4, 0x04, 1), (0xF5, 0x05, 1),
    (0xF6, 0x06, 1), (0xF7, 0x07, 1), (0xF8, 0x08, 1),

    (0x4E, 0x00, 2), (0x0C, 0x04, 2), (0x21, 0x61, 2), (0x22, 0x68, 2),
    (0x23, 0x6F, 2), (0x24, 0x76, 2), (0x25, 0x7D, 2), (0x26, 0x84, 2),
    (0x27, 0x8D, 2), (0x4D, 0x08, 2), (0x50, 0x05, 2), (0x51, 0xF5, 2),
    (0x52, 0x04, 2), (0x53, 0xA0, 2), (0x54, 0x1F, 2), (0x55, 0x23, 2),
    (0x56, 0x45, 2), (0x57, 0x67, 2), (0x58, 0x08, 2), (0x59, 0x08, 2),
    (0x5A, 0x08, 2), (0x5B, 0x08, 2), (0x60, 0x08, 2), (0x61, 0x08, 2),
    (0x62, 0x08, 2), (0x63, 0x08, 2), (0x64, 0xCF, 2),

    (0x5B, 0x40, 0), (0x84, 0x88, 0), (0x85, 0x24, 0), (0x88, 0x54, 0),
    (0x8B, 0xB8, 0), (0x8C, 0x07, 0), (0x8D, 0x00, 0), (0x94, 0x1B, 0),
    (0x95, 0x12, 0), (0x96, 0x00, 0), (0x97, 0x06, 0), (0x9D, 0x1A, 0),
    (0x9F, 0x10, 0), (0xB4, 0x22, 0), (0xBE, 0x80, 0), (0xDB, 0x00, 0),
    (0xEE, 0x00, 0), (0x4C, 0x00, 2),

    (0x9F, 0x00, 3), (0x8C, 0x01, 0), (0x8D, 0x10, 0), (0x8E, 0x08, 0),
    (0x8F, 0x00, 0),
)


def rtl8187b_init_hw(t: RTL8187Transport, rf_init: RFInit) -> None:
    """Port of rtl8187b_init_hw (dev.c:769-881). priv->slot_time / aifsn are not kept: only
    conf_erp / conf_tx read them, and mac80211 calls neither for a monitor vif."""
    set_anaparam(t, rfon=True, is_rtl8187b=True)

    # Reset PLL sequence on 8187B
    t.write8(0xFF61, 0x10)
    reg = t.read8(0xFF62)
    t.write8(0xFF62, reg & ~(1 << 5) & 0xFF)
    t.write8(0xFF62, reg | (1 << 5))

    cmd_reset(t)

    set_anaparam(t, rfon=True, is_rtl8187b=True)

    t.write16(0xFF34, 0x0FFF)                  # BRSR on 8187B: all 12 rates

    reg = t.read8(REG_CW_CONF)
    reg |= CW_CONF_PERPACKET_RETRY
    t.write8(REG_CW_CONF, reg)

    # Auto Rate Fallback Register (ARFR): 1M-54M
    t.write16(0xFFE0, 0x0FFF, 1)
    t.write8(0xFFE2, 0x00, 1)

    t.write16(0xFFD4, 0xFFFF, 1)

    t.write8(REG_EEPROM_CMD, EEPROM_CMD_CONFIG)
    reg = t.read8(REG_CONFIG1)
    t.write8(REG_CONFIG1, (reg & 0x3F) | 0x80)
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_NORMAL)

    t.write8(REG_WPA_CONF, 0)
    for addr, val, idx in rtl8187b_reg_table:
        t.write8(addr | 0xFF00, val, idx)

    t.write16(REG_TID_AC_MAP, 0xFA50)
    t.write16(REG_INT_MIG, 0)

    t.write32(0xFFF0, 0, 1)
    t.write32(0xFFF4, 0, 1)
    t.write8(0xFFF8, 0, 1)

    t.write32(REG_RF_TIMING, 0x00004001)

    t.write16(0xFF72, 0x569A, 2)               # RFSW_CTRL

    t.write16(REG_RFPINSOUTPUT, 0x0480)
    t.write16(REG_RFPINSSELECT, 0x2488)
    t.write16(REG_RFPINSENABLE, 0x1FFF)
    time.sleep(0.100)

    rf_init(t)

    reg = CMD_TX_ENABLE | CMD_RX_ENABLE
    t.write8(REG_CMD, reg)
    t.write16(REG_INT_MASK, 0xFFFF)

    t.write8(0xFE41, 0xF4)
    t.write8(0xFE40, 0x00)
    t.write8(0xFE42, 0x00)
    t.write8(0xFE42, 0x01)
    t.write8(0xFE40, 0x0F)
    t.write8(0xFE42, 0x00)
    t.write8(0xFE42, 0x01)

    reg = t.read8(0xFFDB)
    t.write8(0xFFDB, reg | (1 << 2))
    t.write16(0xFF72, 0x59FA, 3)
    t.write16(0xFF74, 0x59D2, 3)
    t.write16(0xFF76, 0x59D2, 3)
    t.write16(0xFF78, 0x19FA, 3)
    t.write16(0xFF7A, 0x19FA, 3)
    t.write16(0xFF7C, 0x00D0, 3)
    t.write8(0xFF61, 0)
    t.write8(0xFF80, 0x0F, 1)
    t.write8(0xFF83, 0x03, 1)
    t.write8(0xFFDA, 0x10)
    t.write8(0xFF4D, 0x08, 2)

    t.write32(REG_HSSI_PARA, 0x0600321B)

    t.write16(0xFFEC, 0x0800, 1)

    t.write8(REG_ACM_CONTROL, 0)

    # ENEDCA flag must always be set, transmit issues?
    t.write8(REG_MSR, MSR_ENEDCA)


def start(t: RTL8187Transport, is_rtl8187b: bool) -> int:
    """Port of rtl8187_start (dev.c:923-1015) — L-branch. Returns the RX_CONF baseline
    written (the kernel's ``priv->rx_conf``), which :func:`configure_filter` then updates.

    Enables interrupts, opens multicast filters, writes the station-mode RX_CONF baseline
    (BSSID-filtered — *no* monitor bit, exactly as the kernel does), clears the per-packet
    CW / TX_AGC overrides, sets TX_CONF, then latches CMD |= TX_ENABLE|RX_ENABLE so the chip
    starts pushing frames to bulk-IN.

    Monitor mode is NOT entered here: the kernel's airmon path enters it via a *separate*
    ``configure_filter`` write after ``start`` (dev.c:1338). Reproducing that split is what
    makes the wire byte-for-byte — folding the monitor bit into ``start`` (as an earlier
    port did) both mis-orders the write and drops the CTRL bit airmon also requests.

    Pre-condition: init_hw has already run (chip reset, RF programmed, ANAPARAM ON).
    The 8187B branch (dev.c:939-971) leaves CMD alone: rtl8187b_init_hw already enabled TX/RX.
    """
    if is_rtl8187b:
        rx_conf = (
            RX_CONF_MGMT
            | RX_CONF_DATA
            | RX_CONF_BROADCAST
            | RX_CONF_NICMAC
            | RX_CONF_BSSID
            | (7 << 13)
            | (7 << 10)
            | RX_CONF_RX_AUTORESETPHY
            | RX_CONF_ONLYERLPKT
        )
        t.write32(REG_RX_CONF, rx_conf)

        reg = t.read8(REG_TX_AGC_CTL)
        reg &= ~TX_AGC_CTL_PERPACKET_GAIN
        reg &= ~TX_AGC_CTL_PERPACKET_ANTSEL
        reg &= ~TX_AGC_CTL_FEEDBACK_ANT
        t.write8(REG_TX_AGC_CTL, reg & 0xFF)

        t.write32(REG_TX_CONF,
                  TX_CONF_HW_SEQNUM
                  | TX_CONF_DISREQQSIZE
                  | (RETRY_COUNT << 8)      # short retry limit
                  | (RETRY_COUNT << 0)      # long retry limit
                  | (7 << 21))              # MAX TX DMA
        return rx_conf

    t.write16(REG_INT_MASK, 0xFFFF)

    # Multicast: accept everything (0xFFFFFFFF in both MAR halves).
    t.write32(REG_MAR + 0, 0xFFFFFFFF)
    t.write32(REG_MAR + 4, 0xFFFFFFFF)

    # RX_CONF baseline (dev.c:982-990). `(7 << 13)` (RX FIFO threshold NONE) and
    # `(7 << 10)` (MAX RX DMA) live in bit-fields rtl818x.h doesn't name — mirrored verbatim.
    rx_conf = (
        RX_CONF_ONLYERLPKT
        | RX_CONF_RX_AUTORESETPHY
        | RX_CONF_BSSID
        | RX_CONF_MGMT
        | RX_CONF_DATA
        | (7 << 13)
        | (7 << 10)
        | RX_CONF_BROADCAST
        | RX_CONF_NICMAC
    )
    t.write32(REG_RX_CONF, rx_conf)

    # CW_CONF: clear per-packet CW override, set per-packet retry.
    reg = t.read8(REG_CW_CONF)
    reg &= ~(1 << 0)  # ~CW_CONF_PERPACKET_CW
    reg |= (1 << 1)   # CW_CONF_PERPACKET_RETRY
    t.write8(REG_CW_CONF, reg & 0xFF)

    # TX_AGC_CTL: clear per-packet gain/antsel/feedback-ant.
    reg = t.read8(REG_TX_AGC_CTL)
    reg &= ~(1 << 0)  # ~PERPACKET_GAIN
    reg &= ~(1 << 1)  # ~PERPACKET_ANTSEL
    reg &= ~(1 << 2)  # ~FEEDBACK_ANT
    t.write8(REG_TX_AGC_CTL, reg & 0xFF)

    # TX_CONF: CW_MIN + MAX TX DMA (7<<21) + NO_ICV.
    tx_conf = TX_CONF_CW_MIN | (7 << 21) | TX_CONF_NO_ICV
    t.write32(REG_TX_CONF, tx_conf)

    # Latch TX + RX enable. From here bulk-IN produces frames.
    reg = t.read8(REG_CMD)
    reg |= CMD_TX_ENABLE
    reg |= CMD_RX_ENABLE
    t.write8(REG_CMD, reg)
    return rx_conf


def configure_filter(t: RTL8187Transport, rx_conf: int, changed_flags: int, total_flags: int,
                     multicast: int) -> tuple[int, int]:
    """Port of rtl8187_configure_filter (dev.c:1310-1339). Returns the new
    (``priv->rx_conf``, ``*total_flags``); mac80211 passes ``changed_flags`` = last total ^ new."""
    if changed_flags & FIF_FCSFAIL:
        rx_conf ^= RX_CONF_FCS
    if changed_flags & FIF_CONTROL:
        rx_conf ^= RX_CONF_CTRL
    if total_flags & FIF_OTHER_BSS or total_flags & FIF_ALLMULTI or multicast > 0:
        rx_conf |= RX_CONF_MONITOR
    else:
        rx_conf &= ~RX_CONF_MONITOR

    total_flags = 0
    if rx_conf & RX_CONF_FCS:
        total_flags |= FIF_FCSFAIL
    if rx_conf & RX_CONF_CTRL:
        total_flags |= FIF_CONTROL
    if rx_conf & RX_CONF_MONITOR:
        total_flags |= FIF_OTHER_BSS
        total_flags |= FIF_ALLMULTI

    t.write32(REG_RX_CONF, rx_conf)
    return rx_conf, total_flags


def stop(t: RTL8187Transport, rf_stop: Callable[[RTL8187Transport], None],
         is_rtl8187b: bool) -> None:
    """Port of rtl8187_stop (dev.c:1022-1053), register side; ``rf_stop`` is priv->rf->stop."""
    t.write16(REG_INT_MASK, 0)

    reg = t.read8(REG_CMD)
    reg &= ~CMD_TX_ENABLE
    reg &= ~CMD_RX_ENABLE
    t.write8(REG_CMD, reg & 0xFF)

    rf_stop(t)
    set_anaparam(t, rfon=False, is_rtl8187b=is_rtl8187b)

    t.write8(REG_EEPROM_CMD, EEPROM_CMD_CONFIG)
    reg = t.read8(REG_CONFIG4)
    t.write8(REG_CONFIG4, reg | CONFIG4_VCOOFF)
    t.write8(REG_EEPROM_CMD, EEPROM_CMD_NORMAL)


def cold_bring_up(t: RTL8187Transport, rf_init: RFInit, is_rtl8187b: bool) -> int:
    """rtl8187_start (init_hw or rtl8187b_init_hw, then the start writes), then
    configure_filter (enter monitor); returns the monitor RX_CONF."""
    if not is_rtl8187b:
        init_hw(t, rf_init=rf_init)
    else:
        rtl8187b_init_hw(t, rf_init=rf_init)
    rx_conf = start(t, is_rtl8187b)
    # A monitor vif requests FIF_OTHER_BSS | FIF_CONTROL.
    monitor_flags = FIF_OTHER_BSS | FIF_CONTROL
    rx_conf, _ = configure_filter(t, rx_conf, monitor_flags, monitor_flags, 0)
    return rx_conf
