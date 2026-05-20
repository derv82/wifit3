"""RTL8187L MAC-layer bring-up + warm/cold helpers.

Mirrors ``rtl8187_{set_anaparam,cmd_reset,init_hw,start}`` from
``data_dumps/rtl818x-source-v6.18/rtl8187/dev.c``.

The RF init step inside ``init_hw`` (``priv->rf->init(dev)``) is stubbed
in this milestone and ported in M2b — without it the chip's MAC comes
fully online but the receiver is blind because the synthesizer hasn't
been programmed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .constants import (
    ANAPARAM_OFF,
    ANAPARAM_ON,
    ANAPARAM2_OFF,
    ANAPARAM2_ON,
    CMD_RESET,
    CMD_RX_ENABLE,
    CMD_TX_ENABLE,
    CONFIG3_ANAPARAM_WRITE,
    EEPROM_CMD_CONFIG,
    EEPROM_CMD_LOAD,
    EEPROM_CMD_NORMAL,
    HWVER_CHIP_NAMES,
    HWVER_DEFAULT_NAME,
    REG_ANAPARAM,
    REG_ANAPARAM2,
    REG_BRSR,
    REG_CMD,
    REG_CONFIG1,
    REG_CONFIG3,
    REG_CW_CONF,
    REG_EEPROM_CMD,
    REG_GP_ENABLE,
    REG_GPIO0,
    REG_INT_MASK,
    REG_INT_TIMEOUT,
    REG_MAC0,
    REG_MAGIC_FE18,
    REG_MAGIC_FE53,
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
    RX_CONF_DATA,
    RX_CONF_MGMT,
    RX_CONF_MONITOR,
    RX_CONF_NICMAC,
    RX_CONF_ONLYERLPKT,
    RX_CONF_RX_AUTORESETPHY,
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
    hwver_raw: int
    name: str
    is_8187b_masquerade: bool


def detect_chip_variant(t: RTL8187Transport) -> ChipVariant:
    """Read TX_CONF and decode HWVER bits to identify the chip.

    Mirrors the L-branch switch in rtl8187_probe (dev.c:1539-1556).
    """
    tx_conf = t.read32(REG_TX_CONF)
    hwver = tx_conf & TX_CONF_HWVER_MASK
    name = HWVER_CHIP_NAMES.get(hwver, HWVER_DEFAULT_NAME)
    return ChipVariant(
        hwver_raw=hwver,
        name=name,
        is_8187b_masquerade=(hwver == TX_CONF_R8187vD_B),
    )


def read_perm_mac(t: RTL8187Transport) -> bytes:
    """Read the 6-byte permanent MAC from MAC0/MAC4.

    On the 8187L the EEPROM is auto-loaded into MAC0..5 on USB
    enumeration, so this returns the real MAC straight from a cold
    plug. (See M1 hw-test ground truth: AWUS036H reads back
    ``00:c0:ca:4e:ca:e0`` immediately.)
    """
    return t.read_bytes(REG_MAC0, 6)


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
# Stub type for the RF-init callback. M2b will provide a real
# implementation backed by rtl8225_rf_init / rtl8225z2_rf_init.
RFInit = Callable[[RTL8187Transport], None]


def _stub_rf_init(t: RTL8187Transport) -> None:
    """Placeholder for priv->rf->init(dev). Replaced by the real rtl8225
    RF init in M2b. With this no-op the MAC comes fully online but the
    RF synth is unprogrammed → bulk-IN may stay silent until M2b lands."""
    logger.warning("rf.init() stub — receiver will be blind until M2b ports rtl8225_rf_init")


def set_anaparam(t: RTL8187Transport, rfon: bool) -> None:
    """Drive the analogue baseband on/off.

    Mirrors rtl8187_set_anaparam (dev.c:570-608) — L-branch only.

    The CONFIG3 ANAPARAM_WRITE bit is the gate: set it before touching
    ANAPARAM/ANAPARAM2, clear it after. Both register-window ops are
    bracketed by EEPROM_CMD = CONFIG → NORMAL so the analog write
    window is open.
    """
    if rfon:
        anaparam = ANAPARAM_ON
        anaparam2 = ANAPARAM2_ON
    else:
        anaparam = ANAPARAM_OFF
        anaparam2 = ANAPARAM2_OFF

    t.write8(REG_EEPROM_CMD, EEPROM_CMD_CONFIG)
    reg = t.read8(REG_CONFIG3)
    reg |= CONFIG3_ANAPARAM_WRITE
    t.write8(REG_CONFIG3, reg)
    t.write32(REG_ANAPARAM, anaparam)
    t.write32(REG_ANAPARAM2, anaparam2)
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


def init_hw(t: RTL8187Transport, rf_init: RFInit = _stub_rf_init) -> None:
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
      726      priv->rf->init(dev)            [stubbed in M2a; ported M2b]
      728-734  BRSR=0x01F3 + PGSELECT page-1 magic regs (0xFFFE=0x10,
               TALLY_SEL=0x80, 0xFFFF=0x60) bracketed by PGSELECT reg
    """
    # ---- pre-reset analog ON --------------------------------------------------
    set_anaparam(t, rfon=True)
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
    set_anaparam(t, rfon=True)

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

    # ---- RF init (stubbed in M2a, real in M2b) -------------------------------
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


def start(t: RTL8187Transport) -> None:
    """Port of rtl8187_start (dev.c:923-1015) — L-branch.

    Enables interrupts, opens multicast filters, configures RX_CONF for
    monitor-mode-friendly capture (BROADCAST + NICMAC + MGMT + DATA),
    clears TX_AGC_CTL per-packet overrides, sets TX_CONF, then latches
    CMD |= TX_ENABLE|RX_ENABLE so the chip starts pushing frames to
    bulk-IN.

    Pre-condition: init_hw has already run (so the chip is reset, the
    RF lines are configured, and ANAPARAM is ON).
    """
    t.write16(REG_INT_MASK, 0xFFFF)

    # Multicast: accept everything (0xFFFFFFFF in both MAR halves).
    t.write32(REG_MAR + 0, 0xFFFFFFFF)
    t.write32(REG_MAR + 4, 0xFFFFFFFF)

    # RX_CONF: monitor-mode-friendly. Kernel `start` sets the
    # station-mode baseline (with RX_CONF_BSSID — drop anything not
    # addressed to our BSSID), then `configure_filter` ORs in
    # RX_CONF_MONITOR (bit 0) when FIF_OTHER_BSS gets requested. We
    # don't have an upper stack to call configure_filter, so we OR in
    # RX_CONF_MONITOR here at start() — the chip then accepts frames
    # for all BSSIDs (the BSSID filter becomes a hint rather than a
    # gate).  Same lesson as [[feedback_station_vs_monitor_rcr]] on
    # RTL8188EUS M8 — kernel init writes a STATION filter; monitor-
    # mode-by-default needs the all-BSSID bit set explicitly.
    #
    # NB: the kernel ORs in `(7 << 13)` (RX FIFO threshold NONE) and
    # `(7 << 10)` (MAX RX DMA). Those live in bit-fields that aren't
    # named in rtl818x.h — we mirror them verbatim.
    rx_conf = (
        RX_CONF_MONITOR           # bit 0  ← critical for EAPOL visibility
        | RX_CONF_ONLYERLPKT
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

    # Latch TX + RX enable. From this point bulk-IN should produce
    # frames (once M2b lands the RF init — until then the receiver is
    # blind but the FIFO machinery is fully primed).
    reg = t.read8(REG_CMD)
    reg |= CMD_TX_ENABLE
    reg |= CMD_RX_ENABLE
    t.write8(REG_CMD, reg)


def cold_bring_up(t: RTL8187Transport, rf_init: RFInit = _stub_rf_init) -> None:
    """Run the full cold bring-up: init_hw → start.

    Pass `rf_init` from M2b once that lands.  Without it the MAC is up
    but the receiver stays silent.
    """
    init_hw(t, rf_init=rf_init)
    start(t)
