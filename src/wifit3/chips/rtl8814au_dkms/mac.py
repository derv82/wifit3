"""RTL8814AU MAC register configuration (M2a) + post-table MISC init (M2b).

`PHY_MACConfig8814` [SRC rtl8814a_phycfg.c:216] applies the MAC register table
`array_mp_8814a_mac_reg` [SRC hal/phydm/rtl8814a/halhwimg8814a_mac.c] via
`odm_read_and_config_mp_8814a_mac_reg`. That walker supports conditional rows
(addr bit31/bit30), but the 8814A MAC table has none, so it reduces to a flat
`write8(addr, value)` loop — each `u32` pair's value is applied as one byte.
Verified byte-for-byte at [WIRE] cap1 frames 6717+.

`mac_init_misc` then mirrors the hal_init block between PHY_MACConfig8814 and
PHY_BBConfig8814 [SRC usb/usb_halinit.c rtl8814au_hal_init:1168..1198]: queue
priority, page/driver-info sizes, interrupt mask, network type, the WMAC/RCR/EDCA
config, retry, USB aggregation, beacon parameters, burst length, and the final
MACTXEN/MACRXEN enable. STA/AP-oriented values (RCR, NT_LINK_AP, beacon regs) are
ported as the vendor emits them; the monitor-mode RCR/filter rewrites are a later
(RX) milestone. Verified byte-for-byte at [WIRE] cap1 frames 7003..7101.
"""
from __future__ import annotations

from . import constants as C

# (addr, value) pairs, low byte of each u32 entry, in source order.
MAC_REG_TABLE = (
    (0x010, 0x7C), (0x014, 0xDB), (0x016, 0x02), (0x073, 0x10), (0x420, 0x80),
    (0x421, 0x0F), (0x428, 0x0A), (0x429, 0x10), (0x430, 0x00), (0x431, 0x00),
    (0x432, 0x00), (0x433, 0x01), (0x434, 0x04), (0x435, 0x05), (0x436, 0x07),
    (0x437, 0x08), (0x43C, 0x04), (0x43D, 0x05), (0x43E, 0x07), (0x43F, 0x08),
    (0x440, 0x5D), (0x441, 0x01), (0x442, 0x00), (0x444, 0x10), (0x445, 0xF0),
    (0x446, 0x01), (0x447, 0xFE), (0x448, 0x00), (0x449, 0x00), (0x44A, 0x00),
    (0x44B, 0x40), (0x44C, 0x10), (0x44D, 0xF0), (0x44E, 0x3F), (0x44F, 0x00),
    (0x450, 0x00), (0x451, 0x00), (0x452, 0x00), (0x453, 0x40), (0x45E, 0x04),
    (0x49C, 0x10), (0x49D, 0xF0), (0x49E, 0x00), (0x49F, 0x06), (0x4A0, 0xE0),
    (0x4A1, 0x03), (0x4A2, 0x00), (0x4A3, 0x40), (0x4A4, 0x15), (0x4A5, 0xF0),
    (0x4A6, 0x00), (0x4A7, 0x06), (0x4A8, 0xE0), (0x4A9, 0x00), (0x4AA, 0x00),
    (0x4AB, 0x00), (0x7DA, 0x08), (0x1448, 0x06), (0x144A, 0x06), (0x144C, 0x06),
    (0x144E, 0x06), (0x4C8, 0xFF), (0x4C9, 0x08), (0x4CA, 0x3C), (0x4CB, 0x3C),
    (0x4CC, 0xFF), (0x4CD, 0xFF), (0x4CE, 0x01), (0x4CF, 0x08), (0x500, 0x26),
    (0x501, 0xA2), (0x502, 0x2F), (0x503, 0x00), (0x504, 0x28), (0x505, 0xA3),
    (0x506, 0x5E), (0x507, 0x00), (0x508, 0x2B), (0x509, 0xA4), (0x50A, 0x5E),
    (0x50B, 0x00), (0x50C, 0x4F), (0x50D, 0xA4), (0x50E, 0x00), (0x50F, 0x00),
    (0x512, 0x1C), (0x514, 0x0A), (0x516, 0x0A), (0x521, 0x2F), (0x525, 0x47),
    (0x550, 0x10), (0x551, 0x10), (0x559, 0x02), (0x55C, 0x64), (0x55D, 0xFF),
    (0x577, 0x03), (0x5BE, 0x64), (0x604, 0x01), (0x605, 0x30), (0x607, 0x01),
    (0x608, 0x0E), (0x609, 0x2A), (0x60A, 0x00), (0x60C, 0x18), (0x60D, 0x50),
    (0x6A0, 0xFF), (0x6A1, 0xFF), (0x6A2, 0xFF), (0x6A3, 0xFF), (0x6A4, 0xFF),
    (0x6A5, 0xFF), (0x6DE, 0x84), (0x620, 0xFF), (0x621, 0xFF), (0x622, 0xFF),
    (0x623, 0xFF), (0x624, 0xFF), (0x625, 0xFF), (0x626, 0xFF), (0x627, 0xFF),
    (0x638, 0x64), (0x63C, 0x0A), (0x63D, 0x0A), (0x63E, 0x0E), (0x63F, 0x0E),
    (0x640, 0x40), (0x642, 0x40), (0x643, 0x00), (0x652, 0xC8), (0x66E, 0x05),
    (0x700, 0x21), (0x701, 0x43), (0x702, 0x65), (0x703, 0x87), (0x708, 0x21),
    (0x709, 0x43), (0x70A, 0x65), (0x70B, 0x87), (0x718, 0x40), (0x7D5, 0xBC),
    (0x7D8, 0x28), (0x7D9, 0x00), (0x7DA, 0x0B),
)


def phy_mac_config(t) -> None:
    """[SRC] PHY_MACConfig8814 — apply the MAC register table (flat write8 loop)."""
    for addr, value in MAC_REG_TABLE:
        t.write8(addr, value)


# ---------------------------------------------------------------------------
# MISC stage — rtl8814au_hal_init lines 1168..1198 (between MAC and BB config)
# ---------------------------------------------------------------------------

def _init_queue_priority(t) -> None:
    """[SRC] _InitQueuePriority_8814AUsb -> _InitNormalChipThreeOutEpPriority.

    The 8814AU enumerates 3/4 bulk-OUT endpoints, so it takes the three-out-EP
    path with the typical (non-WMM) queue assignment. value16 keeps the low 3
    bits of REG_TRXDMA_CTRL and sets the per-AC queue maps + BIT2.
    """
    be, bk, vi, vo = C.QUEUE_LOW, C.QUEUE_LOW, C.QUEUE_NORMAL, C.QUEUE_HIGH
    mgt = hi = C.QUEUE_HIGH
    v = t.read16(C.REG_TRXDMA_CTRL) & 0x7
    v |= (C._txdma_map(be, 8) | C._txdma_map(bk, 10) | C._txdma_map(vi, 6)
          | C._txdma_map(vo, 4) | C._txdma_map(mgt, 12) | C._txdma_map(hi, 14))
    v |= (1 << 2)
    t.write16(C.REG_TRXDMA_CTRL, v)


def _init_page_boundary(t) -> None:
    """[SRC] _InitPageBoundary_8814AUsb."""
    t.write16(C.REG_RXFF_PTR, C.RX_DMA_BOUNDARY)


def _init_driver_info_size(t) -> None:
    """[SRC] _InitDriverInfoSize_8814A(DRVINFO_SZ)."""
    t.write8(C.REG_RX_DRVINFO_SZ, C.DRVINFO_SZ)


def _init_interrupt(t) -> None:
    """[SRC] _InitInterrupt_8814AU — HIMR0/HIMR1 = IntrMask[0..1] (0 on USB)."""
    t.write32(C.REG_HIMR0, 0)
    t.write32(C.REG_HIMR1, 0)


def _init_network_type(t) -> None:
    """[SRC] _InitNetworkType_8814A — REG_CR network-type field = NT_LINK_AP."""
    v = t.read32(C.REG_CR)
    v = (v & ~C.MASK_NETTYPE) | C.NETTYPE(C.NT_LINK_AP)
    t.write32(C.REG_CR, v)


def _init_mac_configure(t) -> None:
    """[SRC] _InitMacConfigure_8814A (old WMAC + adaptive-ctrl merged)."""
    # RRSR: masked write of the basic-rate bitmap (phydm_rrsr_set_register).
    rrsr = C.RATE_ALL_CCK | C.RATE_ALL_OFDM_AG
    v = t.read32(C.REG_RRSR)
    t.write32(C.REG_RRSR, (v & ~C.RRSR_RATE_MASK) | (rrsr & C.RRSR_RATE_MASK))
    # Retry limit (long|short, both RL_VAL_STA).
    t.write16(C.REG_RETRY_LIMIT, (C.RL_VAL_STA << 8) | C.RL_VAL_STA)
    # RCR (STA init value). HW_VAR_RCR writes it straight to REG_RCR.
    t.write32(C.REG_RCR, C.RCR_INIT_VALUE)
    # RxFilterMap1: mask ps-poll, accept NDPA (beamforming).
    t.write16(C.REG_RXFLTMAP1, C.RXFLTMAP1_VAL)
    # Aggregation caps reduced when RA is enabled.
    t.write8(C.REG_MAX_AGGR_NUM, C.MAX_AGGR_NUM)
    t.write8(C.REG_RTS_MAX_AGGR_NUM, C.MAX_AGGR_NUM)


def _init_edca(t) -> None:
    """[SRC] _InitEDCA_8814AUsb — SIFS + per-AC EDCA parameters."""
    t.write16(C.REG_SPEC_SIFS, C.SIFS_VAL)
    t.write16(C.REG_MAC_SPEC_SIFS, C.SIFS_VAL)
    t.write16(C.REG_SIFS_CTX, C.SIFS_VAL)
    t.write16(C.REG_SIFS_TRX, C.SIFS_VAL)
    t.write32(C.REG_EDCA_BE_PARAM, C.EDCA_BE_VAL)
    t.write32(C.REG_EDCA_BK_PARAM, C.EDCA_BK_VAL)
    t.write32(C.REG_EDCA_VI_PARAM, C.EDCA_VI_VAL)
    t.write32(C.REG_EDCA_VO_PARAM, C.EDCA_VO_VAL)


def _init_retry_function(t) -> None:
    """[SRC] _InitRetryFunction_8814A — AMPDU retry-new + ACK timeout."""
    v = t.read8(C.REG_FWHW_TXQ_CTRL)
    t.write8(C.REG_FWHW_TXQ_CTRL, v | C.EN_AMPDU_RTY_NEW)
    t.write8(C.REG_ACKTO, C.ACKTO_VAL)


def _init_usb_aggregation(t) -> None:
    """[SRC] init_UsbAggregationSetting_8814A — TX desc-num + RX DMA-agg mode.

    TX: REG_TDECTRL block-desc-num = UsbTxAggDescNum (3); +3 byte = num<<1.
    RX: RX_AGG_DMA — set RXDMA_AGG_EN on REG_TRXDMA_CTRL, clear USB_AGG_EN. On a
    cold boot both bits are already in the wanted state, so the values are
    unchanged but the read/write pair is still emitted (ported verbatim).
    """
    v = t.read32(C.REG_TDECTRL)
    v &= ~(C.BLK_DESC_NUM_MASK << C.BLK_DESC_NUM_SHIFT)
    v |= (C.USB_TX_AGG_DESC_NUM & C.BLK_DESC_NUM_MASK) << C.BLK_DESC_NUM_SHIFT
    t.write32(C.REG_TDECTRL, v)
    t.write8(C.REG_TDECTRL + 3, C.USB_TX_AGG_DESC_NUM << 1)

    value_dma = t.read8(C.REG_TRXDMA_CTRL)
    value_usb = t.read8(C.REG_RXDMA_AGG_PG_TH + 3)
    value_dma |= C.RXDMA_AGG_EN          # RX_AGG_DMA
    value_usb &= ~C.USB_AGG_EN
    t.write8(C.REG_TRXDMA_CTRL, value_dma & 0xFF)
    t.write8(C.REG_RXDMA_AGG_PG_TH + 3, value_usb & 0xFF)


def _init_beacon_parameters(t) -> None:
    """[SRC] _InitBeaconParameters_8814A + _InitBeaconMaxError_8814A(TRUE)."""
    val8 = C.DIS_TSF_UDT
    t.write16(C.REG_BCN_CTRL, val8 | (val8 << 8))   # port0 + port1
    t.write8(C.REG_TBTT_PROHIBIT, C.TBTT_PROHIBIT_SETUP_TIME)
    t.write8(C.REG_TBTT_PROHIBIT + 1, C.TBTT_PROHIBIT_HOLD_TIME_STOP_BCN & 0xFF)
    v = t.read8(C.REG_TBTT_PROHIBIT + 2)
    t.write8(C.REG_TBTT_PROHIBIT + 2,
             (v & 0xF0) | (C.TBTT_PROHIBIT_HOLD_TIME_STOP_BCN >> 8))
    t.write8(C.REG_DRVERLYINT, C.DRIVER_EARLY_INT_TIME)
    t.write8(C.REG_BCNDMATIM, C.BCN_DMA_ATIME_INT_TIME)
    t.write16(C.REG_BCNTCFG, C.BCNTCFG_VAL)
    t.write8(C.REG_BCN_MAX_ERR, 0xFF)               # CONFIG_ADHOC_WORKAROUND_SETTING


def _init_burst_pkt_len(t) -> None:
    """[SRC] _InitBurstPktLen — fast-EDCA + USB2 RXDMA burst/agg threshold.

    This card enumerates at USB2 (REG_USB_SPEED bit7 set) with a 512-B bulk-out,
    so burst length = 0x1e and the 20K aggregation threshold applies.
    """
    t.write32(C.REG_FAST_EDCA_VOVI_SETTING, C.FAST_EDCA_VAL)
    t.write32(C.REG_FAST_EDCA_BEBK_SETTING, C.FAST_EDCA_VAL)
    t.read8(C.REG_USB_SPEED)                         # check USB3 vs USB2 (bit7)
    t.write8(C.REG_RXDMA_MODE, C.RXDMA_MODE_BURST_512)
    t.write16(C.REG_RXDMA_AGG_PG_TH, C.RXDMA_AGG_TH_USB2)


def mac_init_misc(t) -> None:
    """hal_init MISC stage between PHY_MACConfig8814 and PHY_BBConfig8814."""
    _init_queue_priority(t)
    _init_page_boundary(t)
    # _InitTransferPageSize_8814AUsb is a no-op on 8814.
    _init_driver_info_size(t)
    _init_interrupt(t)
    _init_network_type(t)
    _init_mac_configure(t)
    _init_edca(t)
    _init_retry_function(t)
    _init_usb_aggregation(t)
    _init_beacon_parameters(t)
    _init_burst_pkt_len(t)
    # Enable MAC TX/RX after the RxFF boundary is set.
    v = t.read8(C.REG_CR)
    t.write8(C.REG_CR, v | C.MACTXEN | C.MACRXEN)
