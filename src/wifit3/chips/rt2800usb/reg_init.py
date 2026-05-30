"""rt2800_init_registers — the bulk MAC configuration step.

Ported verbatim from rt2800lib.c:5836-6374 (M2b-2). RT5392 path only;
other chip variants land later.

Skipped for now (deferred to later milestones):
  * EEPROM-dependent branches (TX_SW_CFG2 DAC_TEST, antenna diversity,
    Bluetooth coex) — need EFUSE bring-up first (see
    [[feedback_defer_efuse_on_bring_up]]).
  * Beacon-slot clears — beaconing not needed for capture.
  * BBP21 reset (RT6352 only, not our chip).

We use a small ``_set_field32(reg, mask, value)`` helper to mirror the
kernel ``rt2x00_set_field32`` so the port reads almost identically to
the C source.
"""
from __future__ import annotations

import logging

from .constants import (
    AGGREGATION_SIZE,
    AMPDU_BA_WINSIZE,
    AUTO_RSP_CFG,
    BCN_TIME_CFG,
    BKOFF_SLOT_CFG,
    CCK_PROT_CFG,
    CH_TIME_CFG,
    EXP_ACK_TIME,
    GF20_PROT_CFG,
    GF40_PROT_CFG,
    HT_BASIC_RATE,
    HT_FBK_CFG0,
    HT_FBK_CFG1,
    INT_TIMER_CFG,
    LED_CFG,
    LEGACY_BASIC_RATE,
    LG_FBK_CFG0,
    LG_FBK_CFG1,
    MAC_IVEIV_TABLE_BASE,
    MAC_SYS_CTRL,
    MAX_LEN_CFG,
    MM20_PROT_CFG,
    MM40_PROT_CFG,
    OFDM_PROT_CFG,
    PBF_CFG,
    PBF_MAX_PCNT,
    PWR_PIN_CFG,
    RT_RT5592,
    RX_STA_CNT0,
    RX_STA_CNT1,
    RX_STA_CNT2,
    SHARED_KEY_MODE_BASE,
    TX_LINK_CFG,
    TX_RTS_CFG,
    TX_RTY_CFG,
    TX_STA_CNT0,
    TX_STA_CNT1,
    TX_STA_CNT2,
    TX_SW_CFG0,
    TX_SW_CFG1,
    TX_SW_CFG2,
    TX_TIMEOUT_CFG,
    TXOP_CTRL_CFG,
    TXOP_HLDR_ET,
    US_CYC_CNT,
    USB_MAX_PSDU,
    WPDMA_GLO_CFG,
    WPDMA_GLO_CFG_ENABLE_RX_DMA,
    WPDMA_GLO_CFG_ENABLE_TX_DMA,
    WPDMA_GLO_CFG_RX_DMA_BUSY,
    WPDMA_GLO_CFG_TX_DMA_BUSY,
    WPDMA_GLO_CFG_TX_WRITEBACK_DONE,
    XIFS_TIME_CFG,
)
from .firmware import disable_wpdma
from .transport import RT2800USBTransport

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Helper — mirror of kernel `rt2x00_set_field32(&reg, mask, value)`.
# ----------------------------------------------------------------------
def _set_field32(reg: int, mask: int, value: int) -> int:
    """Replace the bits of ``mask`` in ``reg`` with ``value`` shifted to
    land in ``mask``. Returns the new u32 register value."""
    shift = (mask & -mask).bit_length() - 1
    return ((reg & ~mask) | ((value << shift) & mask)) & 0xFFFFFFFF


# Bit-field masks needed by init_registers (one mask per kernel
# `rt2x00_set_field32` call we'll port). Direct copies of FIELD32 defs
# from rt2800.h — addresses already moved to constants.py.

# BCN_TIME_CFG
_BCN_TIME_CFG_BEACON_INTERVAL = 0x0000FFFF
_BCN_TIME_CFG_TSF_TICKING = 0x00010000
_BCN_TIME_CFG_TSF_SYNC = 0x00060000
_BCN_TIME_CFG_TBTT_ENABLE = 0x00080000
_BCN_TIME_CFG_BEACON_GEN = 0x00100000
_BCN_TIME_CFG_TX_TIME_COMPENSATE = 0xF0000000

# BKOFF_SLOT_CFG
_BKOFF_SLOT_CFG_SLOT_TIME = 0x000000FF
_BKOFF_SLOT_CFG_CC_DELAY_TIME = 0x0000FF00

# TX_LINK_CFG
_TX_LINK_CFG_REMOTE_MFB_LIFETIME = 0x000000FF
_TX_LINK_CFG_MFB_ENABLE = 0x00000100
_TX_LINK_CFG_REMOTE_UMFS_ENABLE = 0x00000200
_TX_LINK_CFG_TX_MRQ_EN = 0x00000400
_TX_LINK_CFG_TX_RDG_EN = 0x00000800
_TX_LINK_CFG_TX_CF_ACK_EN = 0x00001000
_TX_LINK_CFG_REMOTE_MFB = 0x00FF0000
_TX_LINK_CFG_REMOTE_MFS = 0xFF000000

# TX_TIMEOUT_CFG
_TX_TIMEOUT_CFG_MPDU_LIFETIME = 0x000000F0
_TX_TIMEOUT_CFG_RX_ACK_TIMEOUT = 0x0000FF00
_TX_TIMEOUT_CFG_TX_OP_TIMEOUT = 0x00FF0000

# MAX_LEN_CFG
_MAX_LEN_CFG_MAX_MPDU = 0x00000FFF
_MAX_LEN_CFG_MAX_PSDU = 0x00003000
_MAX_LEN_CFG_MIN_PSDU = 0x0000C000
_MAX_LEN_CFG_MIN_MPDU = 0x000F0000

# LED_CFG
_LED_CFG_ON_PERIOD = 0x000000FF
_LED_CFG_OFF_PERIOD = 0x0000FF00
_LED_CFG_SLOW_BLINK_PERIOD = 0x003F0000
_LED_CFG_R_LED_MODE = 0x03000000
_LED_CFG_G_LED_MODE = 0x0C000000
_LED_CFG_Y_LED_MODE = 0x30000000
_LED_CFG_LED_POLAR = 0x40000000

# TX_RTY_CFG
_TX_RTY_CFG_SHORT_RTY_LIMIT = 0x000000FF
_TX_RTY_CFG_LONG_RTY_LIMIT = 0x0000FF00
_TX_RTY_CFG_LONG_RTY_THRE = 0x0FFF0000
_TX_RTY_CFG_NON_AGG_RTY_MODE = 0x10000000
_TX_RTY_CFG_AGG_RTY_MODE = 0x20000000
_TX_RTY_CFG_TX_AUTO_FB_ENABLE = 0x40000000

# AUTO_RSP_CFG
_AUTO_RSP_CFG_AUTORESPONDER = 0x00000001
_AUTO_RSP_CFG_BAC_ACK_POLICY = 0x00000002
_AUTO_RSP_CFG_CTS_40_MMODE = 0x00000004
_AUTO_RSP_CFG_CTS_40_MREF = 0x00000008
_AUTO_RSP_CFG_AR_PREAMBLE = 0x00000010
_AUTO_RSP_CFG_DUAL_CTS_EN = 0x00000040
_AUTO_RSP_CFG_ACK_CTS_PSM_BIT = 0x00000080

# {CCK,OFDM,MM20,MM40,GF20,GF40}_PROT_CFG — same bit layout across all six.
_PROT_CFG_PROTECT_RATE = 0x0000FFFF
_PROT_CFG_PROTECT_CTRL = 0x00030000
_PROT_CFG_PROTECT_NAV_SHORT = 0x00040000
_PROT_CFG_TX_OP_ALLOW_CCK = 0x00100000
_PROT_CFG_TX_OP_ALLOW_OFDM = 0x00200000
_PROT_CFG_TX_OP_ALLOW_MM20 = 0x00400000
_PROT_CFG_TX_OP_ALLOW_MM40 = 0x00800000
_PROT_CFG_TX_OP_ALLOW_GF20 = 0x01000000
_PROT_CFG_TX_OP_ALLOW_GF40 = 0x02000000
_PROT_CFG_RTS_TH_EN = 0x04000000

# TXOP_CTRL_CFG
_TXOP_CTRL_CFG_TIMEOUT_TRUN_EN = 0x00000001
_TXOP_CTRL_CFG_AC_TRUN_EN = 0x00000002
_TXOP_CTRL_CFG_TXRATEGRP_TRUN_EN = 0x00000004
_TXOP_CTRL_CFG_USER_MODE_TRUN_EN = 0x00000008
_TXOP_CTRL_CFG_MIMO_PS_TRUN_EN = 0x00000010
_TXOP_CTRL_CFG_RESERVED_TRUN_EN = 0x00000020
_TXOP_CTRL_CFG_LSIG_TXOP_EN = 0x00000040
_TXOP_CTRL_CFG_EXT_CCA_EN = 0x00000080
_TXOP_CTRL_CFG_EXT_CCA_DLY = 0x0000FF00
_TXOP_CTRL_CFG_EXT_CWMIN = 0x000F0000

# TX_RTS_CFG
_TX_RTS_CFG_AUTO_RTS_RETRY_LIMIT = 0x000000FF
_TX_RTS_CFG_RTS_THRES = 0x00FFFF00
_TX_RTS_CFG_RTS_FBK_EN = 0x01000000
_IEEE80211_MAX_RTS_THRESHOLD = 2347       # 11-bit standard value

# XIFS_TIME_CFG
_XIFS_TIME_CFG_CCKM_SIFS_TIME = 0x000000FF
_XIFS_TIME_CFG_OFDM_SIFS_TIME = 0x0000FF00
_XIFS_TIME_CFG_OFDM_XIFS_TIME = 0x000F0000
_XIFS_TIME_CFG_EIFS = 0x1FF00000
_XIFS_TIME_CFG_BB_RXEND_ENABLE = 0x20000000

# US_CYC_CNT
_US_CYC_CNT_CLOCK_CYCLE = 0x000000FF

# WPDMA_GLO_CFG (M2b-2 sets the full word)
_WPDMA_GLO_CFG_WP_DMA_BURST_SIZE = 0x00000030

# HT/LG FBK_CFG — masks are nibble-aligned per-MCS
_FBK_MCS0 = 0x0000000F
_FBK_MCS1 = 0x000000F0
_FBK_MCS2 = 0x00000F00
_FBK_MCS3 = 0x0000F000
_FBK_MCS4 = 0x000F0000
_FBK_MCS5 = 0x00F00000
_FBK_MCS6 = 0x0F000000
_FBK_MCS7 = 0xF0000000

# AMPDU_BA_WINSIZE
_AMPDU_BA_WINSIZE_FORCE_WINSIZE_ENABLE = 0x00000020
_AMPDU_BA_WINSIZE_FORCE_WINSIZE = 0x0000001F

# INT_TIMER_CFG
_INT_TIMER_CFG_PRE_TBTT_TIMER = 0x0000FFFF

# CH_TIME_CFG
_CH_TIME_CFG_EIFS_BUSY = 0x00000010
_CH_TIME_CFG_NAV_BUSY = 0x00000008
_CH_TIME_CFG_RX_BUSY = 0x00000004
_CH_TIME_CFG_TX_BUSY = 0x00000002
_CH_TIME_CFG_TMR_EN = 0x00000001


# ----------------------------------------------------------------------
# Big MAC init
# ----------------------------------------------------------------------
def init_registers(t: RT2800USBTransport, silicon_id: int) -> None:
    """Port of rt2800_init_registers (rt2800lib.c:5836-6374).

    Verbatim register-write sequence — every kernel ``rt2x00_set_field32``
    or ``rt2800_register_write`` is mirrored here, in the same order.
    The function ends with ``CH_TIME_CFG`` enabling channel-busy timers,
    matching the kernel return path.
    """
    # 1) Disable WPDMA up-front. M2b-1 already did this but kernel
    # explicitly re-disables here, so we do too (idempotent).
    disable_wpdma(t)

    # 2) drv_init_registers — rt2800usb_init_registers (M2b-1) already
    # ran in the connect() flow before us. Skip re-running.

    # 3) Basic rate sets.
    t.write32(LEGACY_BASIC_RATE, 0x0000013F)
    t.write32(HT_BASIC_RATE, 0x00008003)

    t.write32(MAC_SYS_CTRL, 0x00000000)

    # 4) BCN_TIME_CFG
    reg = t.read32(BCN_TIME_CFG)
    reg = _set_field32(reg, _BCN_TIME_CFG_BEACON_INTERVAL, 1600)
    reg = _set_field32(reg, _BCN_TIME_CFG_TSF_TICKING, 0)
    reg = _set_field32(reg, _BCN_TIME_CFG_TSF_SYNC, 0)
    reg = _set_field32(reg, _BCN_TIME_CFG_TBTT_ENABLE, 0)
    reg = _set_field32(reg, _BCN_TIME_CFG_BEACON_GEN, 0)
    reg = _set_field32(reg, _BCN_TIME_CFG_TX_TIME_COMPENSATE, 0)
    t.write32(BCN_TIME_CFG, reg)

    # 5) rt2800_config_filter(FIF_ALLMULTI) — opens RX filter for all
    # multicast. Lands in M3 with the rest of RX config; skip for now.

    # 6) BKOFF_SLOT_CFG
    reg = t.read32(BKOFF_SLOT_CFG)
    reg = _set_field32(reg, _BKOFF_SLOT_CFG_SLOT_TIME, 9)
    reg = _set_field32(reg, _BKOFF_SLOT_CFG_CC_DELAY_TIME, 2)
    t.write32(BKOFF_SLOT_CFG, reg)

    # 7) TX_SW_CFG0/1/2 — silicon-specific vendor-magic TX timing regs.
    # Each silicon has different values; using the wrong values causes
    # the chip to misconfigure TX timings, sometimes producing
    # TX_SUCCESS=1 in TX_STA_FIFO but no actual on-air RF emission.
    # Discovered 2026-05-22: had previously hardcoded the RT5390/RT5392
    # values for ALL silicons, which is wrong for RT3572 (TX_SW_CFG0
    # bit 2 differs) — broke deauth/PMKID injection on AWUS051NH v2.
    # [SRC] rt2800lib.c:5965-5994
    from .constants import RT_RT3572, RT_RT5390, RT_RT5392
    if silicon_id == RT_RT3572:
        t.write32(TX_SW_CFG0, 0x00000400)   # bit 2 = 0, NOT 0x404
        t.write32(TX_SW_CFG1, 0x00080606)
        # Kernel deliberately does NOT write TX_SW_CFG2 for RT3572 —
        # it falls through the else-if chain after the SW_CFG1 write.
    elif silicon_id in (RT_RT5390, RT_RT5392):
        t.write32(TX_SW_CFG0, 0x00000404)
        t.write32(TX_SW_CFG1, 0x00080606)
        t.write32(TX_SW_CFG2, 0x00000000)
    elif silicon_id == RT_RT5592:
        t.write32(TX_SW_CFG0, 0x00000404)
        t.write32(TX_SW_CFG1, 0x00000000)
        t.write32(TX_SW_CFG2, 0x00000000)
    else:
        # Fallback — generic chip (kernel rt2800lib.c:6026-6028).
        t.write32(TX_SW_CFG0, 0x00000000)
        t.write32(TX_SW_CFG1, 0x00080606)

    # 8) TX_LINK_CFG
    reg = t.read32(TX_LINK_CFG)
    reg = _set_field32(reg, _TX_LINK_CFG_REMOTE_MFB_LIFETIME, 32)
    reg = _set_field32(reg, _TX_LINK_CFG_MFB_ENABLE, 0)
    reg = _set_field32(reg, _TX_LINK_CFG_REMOTE_UMFS_ENABLE, 0)
    reg = _set_field32(reg, _TX_LINK_CFG_TX_MRQ_EN, 0)
    reg = _set_field32(reg, _TX_LINK_CFG_TX_RDG_EN, 0)
    reg = _set_field32(reg, _TX_LINK_CFG_TX_CF_ACK_EN, 1)
    reg = _set_field32(reg, _TX_LINK_CFG_REMOTE_MFB, 0)
    reg = _set_field32(reg, _TX_LINK_CFG_REMOTE_MFS, 0)
    t.write32(TX_LINK_CFG, reg)

    # 9) TX_TIMEOUT_CFG
    reg = t.read32(TX_TIMEOUT_CFG)
    reg = _set_field32(reg, _TX_TIMEOUT_CFG_MPDU_LIFETIME, 9)
    reg = _set_field32(reg, _TX_TIMEOUT_CFG_RX_ACK_TIMEOUT, 32)
    reg = _set_field32(reg, _TX_TIMEOUT_CFG_TX_OP_TIMEOUT, 10)
    t.write32(TX_TIMEOUT_CFG, reg)

    # 10) MAX_LEN_CFG — USB max_psdu = 3
    reg = t.read32(MAX_LEN_CFG)
    reg = _set_field32(reg, _MAX_LEN_CFG_MAX_MPDU, AGGREGATION_SIZE)
    reg = _set_field32(reg, _MAX_LEN_CFG_MAX_PSDU, USB_MAX_PSDU)
    reg = _set_field32(reg, _MAX_LEN_CFG_MIN_PSDU, 10)
    reg = _set_field32(reg, _MAX_LEN_CFG_MIN_MPDU, 10)
    t.write32(MAX_LEN_CFG, reg)

    # 11) LED_CFG
    reg = t.read32(LED_CFG)
    reg = _set_field32(reg, _LED_CFG_ON_PERIOD, 70)
    reg = _set_field32(reg, _LED_CFG_OFF_PERIOD, 30)
    reg = _set_field32(reg, _LED_CFG_SLOW_BLINK_PERIOD, 3)
    reg = _set_field32(reg, _LED_CFG_R_LED_MODE, 3)
    reg = _set_field32(reg, _LED_CFG_G_LED_MODE, 3)
    reg = _set_field32(reg, _LED_CFG_Y_LED_MODE, 3)
    reg = _set_field32(reg, _LED_CFG_LED_POLAR, 1)
    t.write32(LED_CFG, reg)

    # 12) PBF_MAX_PCNT
    t.write32(PBF_MAX_PCNT, 0x1F3FBF9F)

    # 13) TX_RTY_CFG
    reg = t.read32(TX_RTY_CFG)
    reg = _set_field32(reg, _TX_RTY_CFG_SHORT_RTY_LIMIT, 2)
    reg = _set_field32(reg, _TX_RTY_CFG_LONG_RTY_LIMIT, 2)
    reg = _set_field32(reg, _TX_RTY_CFG_LONG_RTY_THRE, 2000)
    reg = _set_field32(reg, _TX_RTY_CFG_NON_AGG_RTY_MODE, 0)
    reg = _set_field32(reg, _TX_RTY_CFG_AGG_RTY_MODE, 0)
    reg = _set_field32(reg, _TX_RTY_CFG_TX_AUTO_FB_ENABLE, 1)
    t.write32(TX_RTY_CFG, reg)

    # 14) AUTO_RSP_CFG
    reg = t.read32(AUTO_RSP_CFG)
    reg = _set_field32(reg, _AUTO_RSP_CFG_AUTORESPONDER, 1)
    reg = _set_field32(reg, _AUTO_RSP_CFG_BAC_ACK_POLICY, 1)
    reg = _set_field32(reg, _AUTO_RSP_CFG_CTS_40_MMODE, 1)
    reg = _set_field32(reg, _AUTO_RSP_CFG_CTS_40_MREF, 0)
    reg = _set_field32(reg, _AUTO_RSP_CFG_AR_PREAMBLE, 0)
    reg = _set_field32(reg, _AUTO_RSP_CFG_DUAL_CTS_EN, 0)
    reg = _set_field32(reg, _AUTO_RSP_CFG_ACK_CTS_PSM_BIT, 0)
    t.write32(AUTO_RSP_CFG, reg)

    # 15) Six PROT_CFG registers — same shape, slightly different values.
    _write_prot_cfg(t, CCK_PROT_CFG,
                    rate=3, ctrl=0, allow_mm40=0, allow_gf40=0)
    _write_prot_cfg(t, OFDM_PROT_CFG,
                    rate=3, ctrl=0, allow_mm40=0, allow_gf40=0)
    _write_prot_cfg(t, MM20_PROT_CFG,
                    rate=0x4004, ctrl=1, allow_cck=0, allow_mm40=0, allow_gf40=0)
    _write_prot_cfg(t, MM40_PROT_CFG,
                    rate=0x4084, ctrl=1, allow_cck=0)
    _write_prot_cfg(t, GF20_PROT_CFG,
                    rate=0x4004, ctrl=1, allow_cck=0, allow_mm40=0, allow_gf40=0)
    _write_prot_cfg(t, GF40_PROT_CFG,
                    rate=0x4084, ctrl=1, allow_cck=0)

    # 16) USB-only path: PBF_CFG + WPDMA_GLO_CFG.
    t.write32(PBF_CFG, 0x00F40006)

    reg = t.read32(WPDMA_GLO_CFG)
    reg &= ~(
        WPDMA_GLO_CFG_ENABLE_TX_DMA
        | WPDMA_GLO_CFG_TX_DMA_BUSY
        | WPDMA_GLO_CFG_ENABLE_RX_DMA
        | WPDMA_GLO_CFG_RX_DMA_BUSY
        | WPDMA_GLO_CFG_TX_WRITEBACK_DONE
        | (0xFF << 7)       # BIG_ENDIAN + RX_HDR_SCATTER
        | (0xFFFF << 16)    # HDR_SEG_LEN
    )
    reg = _set_field32(reg, _WPDMA_GLO_CFG_WP_DMA_BURST_SIZE, 3)
    t.write32(WPDMA_GLO_CFG, reg & 0xFFFFFFFF)

    # 17) TXOP_CTRL_CFG
    reg = t.read32(TXOP_CTRL_CFG)
    reg = _set_field32(reg, _TXOP_CTRL_CFG_TIMEOUT_TRUN_EN, 1)
    reg = _set_field32(reg, _TXOP_CTRL_CFG_AC_TRUN_EN, 1)
    reg = _set_field32(reg, _TXOP_CTRL_CFG_TXRATEGRP_TRUN_EN, 1)
    reg = _set_field32(reg, _TXOP_CTRL_CFG_USER_MODE_TRUN_EN, 1)
    reg = _set_field32(reg, _TXOP_CTRL_CFG_MIMO_PS_TRUN_EN, 1)
    reg = _set_field32(reg, _TXOP_CTRL_CFG_RESERVED_TRUN_EN, 1)
    reg = _set_field32(reg, _TXOP_CTRL_CFG_LSIG_TXOP_EN, 0)
    reg = _set_field32(reg, _TXOP_CTRL_CFG_EXT_CCA_EN, 0)
    reg = _set_field32(reg, _TXOP_CTRL_CFG_EXT_CCA_DLY, 88)
    reg = _set_field32(reg, _TXOP_CTRL_CFG_EXT_CWMIN, 0)
    t.write32(TXOP_CTRL_CFG, reg)

    # 18) TXOP_HLDR_ET — value differs for RT5592.
    t.write32(TXOP_HLDR_ET, 0x00000082 if silicon_id == RT_RT5592 else 0x00000002)

    # 19) TX_RTS_CFG
    reg = t.read32(TX_RTS_CFG)
    reg = _set_field32(reg, _TX_RTS_CFG_AUTO_RTS_RETRY_LIMIT, 7)
    reg = _set_field32(reg, _TX_RTS_CFG_RTS_THRES, _IEEE80211_MAX_RTS_THRESHOLD)
    reg = _set_field32(reg, _TX_RTS_CFG_RTS_FBK_EN, 1)
    t.write32(TX_RTS_CFG, reg)

    # 20) EXP_ACK_TIME (direct write).
    t.write32(EXP_ACK_TIME, 0x002400CA)

    # 21) XIFS_TIME_CFG
    reg = t.read32(XIFS_TIME_CFG)
    reg = _set_field32(reg, _XIFS_TIME_CFG_CCKM_SIFS_TIME, 16)
    reg = _set_field32(reg, _XIFS_TIME_CFG_OFDM_SIFS_TIME, 16)
    reg = _set_field32(reg, _XIFS_TIME_CFG_OFDM_XIFS_TIME, 4)
    reg = _set_field32(reg, _XIFS_TIME_CFG_EIFS, 314)
    reg = _set_field32(reg, _XIFS_TIME_CFG_BB_RXEND_ENABLE, 1)
    t.write32(XIFS_TIME_CFG, reg)

    # 22) PWR_PIN_CFG
    t.write32(PWR_PIN_CFG, 0x00000003)

    # 23) Crypto-table reset. Kernel rt2800_init_registers zeros THREE
    # tables before any key is programmed (rt2800lib.c:6241-6257); each
    # gates the TX crypto engine, so leaving any one set leaves the
    # engine armed for a key we never install. On a Protected (WEP)
    # inject the chip then runs its crypto path and overwrites the
    # frame's IV with a zeroed descriptor IV, so the AP's ICV check
    # drops every frame even though TX_STA_FIFO still flags TX_SUCCESS.
    # [HW] without the SHARED_KEY_MODE clear the RT5572 puts WEP
    # ARP-replay frames on air with IV=00:00:00 and the AP never relays.
    #
    #   (a) SHARED_KEY_MODE[0..3] = 0 — cipher type per BSS index; THE
    #       table that gates WEP (shared-key) replay. [SRC] :6242-6243
    #   (b) WCID (8B = 0xFF "no station" sentinel) + WCID_ATTR (4B = 0,
    #       cipher NONE) per entry. [SRC] :6245-6253, rt2800_config_wcid
    #   (c) IVEIV[0..255] = 0 — the per-WCID IV the engine inserts.
    #       [SRC] :6256-6257
    # ~520 one-shot ~1ms control writes; acceptable startup cost.
    for i in range(4):
        t.write32(SHARED_KEY_MODE_BASE + i * 4, 0)
    _MAC_WCID_BASE = 0x1800
    _MAC_WCID_ATTRIBUTE_BASE = 0x6800
    for i in range(256):
        # 0xFF, not zeros: 0xFF is the "no real station" sentinel; zeros
        # claim MAC 00:00:00:00:00:00 and can skew rate/PA-gain lookups
        # even though TX_STA_FIFO still flags TX_SUCCESS. The 4-byte attr
        # = 0 clears the CIPHER bits (no per-station crypto on this WCID).
        # [SRC] rt2800lib.c:1671-1686
        t.write32(_MAC_WCID_BASE + i * 8, 0xFFFFFFFF)
        t.write32(_MAC_WCID_BASE + i * 8 + 4, 0xFFFFFFFF)
        t.write32(_MAC_WCID_ATTRIBUTE_BASE + i * 4, 0)
    for i in range(256):
        # IVEIV entry is 8 bytes; kernel zeros the first word of each.
        t.write32(MAC_IVEIV_TABLE_BASE + i * 8, 0)

    # 24) USB clock-cycle config.
    reg = t.read32(US_CYC_CNT)
    reg = _set_field32(reg, _US_CYC_CNT_CLOCK_CYCLE, 30)
    t.write32(US_CYC_CNT, reg)

    # 25) HT_FBK_CFG0 — MCS0..7 fall-back table.
    reg = t.read32(HT_FBK_CFG0)
    for mcs, fbk in enumerate((0, 0, 1, 2, 3, 4, 5, 6)):
        mask = _FBK_MCS0 << (mcs * 4)
        reg = _set_field32(reg, mask, fbk)
    t.write32(HT_FBK_CFG0, reg)

    # 26) HT_FBK_CFG1 — MCS8..15 fall-back.
    reg = t.read32(HT_FBK_CFG1)
    for mcs, fbk in enumerate((8, 8, 9, 10, 11, 12, 13, 14)):
        mask = _FBK_MCS0 << (mcs * 4)
        reg = _set_field32(reg, mask, fbk)
    t.write32(HT_FBK_CFG1, reg)

    # 27) LG_FBK_CFG0 — OFDMMCS0..7 fall-back.
    reg = t.read32(LG_FBK_CFG0)
    for mcs, fbk in enumerate((8, 8, 9, 10, 11, 12, 13, 14)):
        mask = _FBK_MCS0 << (mcs * 4)
        reg = _set_field32(reg, mask, fbk)
    t.write32(LG_FBK_CFG0, reg)

    # 28) LG_FBK_CFG1 — CCKMCS0..3 fall-back (only 4 entries used).
    reg = t.read32(LG_FBK_CFG1)
    for mcs, fbk in enumerate((0, 0, 1, 2)):
        mask = _FBK_MCS0 << (mcs * 4)
        reg = _set_field32(reg, mask, fbk)
    t.write32(LG_FBK_CFG1, reg)

    # 29) AMPDU_BA_WINSIZE — disable forced window size.
    reg = t.read32(AMPDU_BA_WINSIZE)
    reg = _set_field32(reg, _AMPDU_BA_WINSIZE_FORCE_WINSIZE_ENABLE, 0)
    reg = _set_field32(reg, _AMPDU_BA_WINSIZE_FORCE_WINSIZE, 0)
    t.write32(AMPDU_BA_WINSIZE, reg)

    # 30) Read-to-clear error counters. The kernel comment notes these
    # registers are clear-on-read.
    for addr in (RX_STA_CNT0, RX_STA_CNT1, RX_STA_CNT2,
                 TX_STA_CNT0, TX_STA_CNT1, TX_STA_CNT2):
        t.read32(addr)

    # 31) INT_TIMER_CFG — 6ms pre-TBTT lead time.
    reg = t.read32(INT_TIMER_CFG)
    reg = _set_field32(reg, _INT_TIMER_CFG_PRE_TBTT_TIMER, 6 << 4)
    t.write32(INT_TIMER_CFG, reg)

    # 32) CH_TIME_CFG — enable channel-busy timers (for stats).
    reg = t.read32(CH_TIME_CFG)
    reg = _set_field32(reg, _CH_TIME_CFG_EIFS_BUSY, 1)
    reg = _set_field32(reg, _CH_TIME_CFG_NAV_BUSY, 1)
    reg = _set_field32(reg, _CH_TIME_CFG_RX_BUSY, 1)
    reg = _set_field32(reg, _CH_TIME_CFG_TX_BUSY, 1)
    reg = _set_field32(reg, _CH_TIME_CFG_TMR_EN, 1)
    t.write32(CH_TIME_CFG, reg)


def _write_prot_cfg(
    t: RT2800USBTransport,
    addr: int,
    *,
    rate: int,
    ctrl: int,
    allow_cck: int = 1,
    allow_ofdm: int = 1,
    allow_mm20: int = 1,
    allow_mm40: int = 1,
    allow_gf20: int = 1,
    allow_gf40: int = 1,
) -> None:
    """Shared body for the six PROT_CFG registers — same bit layout."""
    reg = t.read32(addr)
    reg = _set_field32(reg, _PROT_CFG_PROTECT_RATE, rate)
    reg = _set_field32(reg, _PROT_CFG_PROTECT_CTRL, ctrl)
    reg = _set_field32(reg, _PROT_CFG_PROTECT_NAV_SHORT, 1)
    reg = _set_field32(reg, _PROT_CFG_TX_OP_ALLOW_CCK, allow_cck)
    reg = _set_field32(reg, _PROT_CFG_TX_OP_ALLOW_OFDM, allow_ofdm)
    reg = _set_field32(reg, _PROT_CFG_TX_OP_ALLOW_MM20, allow_mm20)
    reg = _set_field32(reg, _PROT_CFG_TX_OP_ALLOW_MM40, allow_mm40)
    reg = _set_field32(reg, _PROT_CFG_TX_OP_ALLOW_GF20, allow_gf20)
    reg = _set_field32(reg, _PROT_CFG_TX_OP_ALLOW_GF40, allow_gf40)
    reg = _set_field32(reg, _PROT_CFG_RTS_TH_EN, 0)
    t.write32(addr, reg)
