"""
MT7921AU MAC-layer register init.

A faithful port of mt7921_mac_init (mt7921/init.c) and mt792x_mac_init_band
(mt792x_mac.c). Every register here is reached over the unified bus
(read_reg32_unified / write_reg32_unified) — confirmed from the cold-boot
capture, where each of these writes appears as a 0x5F vendor control transfer.

The kernel register helpers (mt76_rmw / mt76_set / mt76_clear / mt76_poll) are
reproduced exactly: each does a read-modify-write and *always* emits the write,
so the on-wire op count matches even when a value is unchanged.
"""
import logging

# ruff: noqa: F403, F405
from .constants import *

logger = logging.getLogger(__name__)


def _ffs(mask: int) -> int:
    """Bit position of the lowest set bit (Linux FIELD_PREP shift)."""
    return (mask & -mask).bit_length() - 1


def _field_prep(mask: int, val: int) -> int:
    return (val << _ffs(mask)) & mask


def rmw(t, addr: int, clear: int, set_: int) -> int:
    """mt76_rmw: new = (read & ~clear) | set; always writes."""
    new = (t.read_reg32_unified(addr) & ~clear) | set_
    t.write_reg32_unified(addr, new)
    return new


def rmw_field(t, addr: int, mask: int, val: int) -> int:
    """mt76_rmw_field: replace the masked field with FIELD_PREP(mask, val)."""
    return rmw(t, addr, mask, _field_prep(mask, val))


def set_bits(t, addr: int, mask: int) -> int:
    """mt76_set: OR in mask."""
    return rmw(t, addr, 0, mask)


def clear_bits(t, addr: int, mask: int) -> int:
    """mt76_clear: AND out mask."""
    return rmw(t, addr, mask, 0)


def poll(t, addr: int, mask: int, val: int, tries: int = 500) -> bool:
    """__mt76_poll: read until (reg & mask) == val. At least one read."""
    for _ in range(tries):
        if (t.read_reg32_unified(addr) & mask) == val:
            return True
    return False


def wtbl_update(t, idx: int, mask: int) -> bool:
    """mt7921_mac_wtbl_update: stamp WLAN_IDX|mask, then wait for BUSY to clear."""
    rmw(t, MT_WTBL_UPDATE, MT_WTBL_UPDATE_WLAN_IDX,
        _field_prep(MT_WTBL_UPDATE_WLAN_IDX, idx) | mask)
    return poll(t, MT_WTBL_UPDATE, MT_WTBL_UPDATE_BUSY, 0)


def mac_init_band(t, band: int) -> None:
    """mt792x_mac_init_band."""
    rmw_field(t, MT_TMAC_CTCR0(band), MT_TMAC_CTCR0_INS_DDLMT_REFTIME, 0x3F)
    set_bits(t, MT_TMAC_CTCR0(band),
             MT_TMAC_CTCR0_INS_DDLMT_VHT_SMPDU_EN | MT_TMAC_CTCR0_INS_DDLMT_EN)

    set_bits(t, MT_WF_RMAC_MIB_TIME0(band), MT_WF_RMAC_MIB_RXTIME_EN)
    set_bits(t, MT_WF_RMAC_MIB_AIRTIME0(band), MT_WF_RMAC_MIB_RXTIME_EN)

    # enable MIB tx/rx time reporting
    set_bits(t, MT_MIB_SCR1(band), MT_MIB_TXDUR_EN)
    set_bits(t, MT_MIB_SCR1(band), MT_MIB_RXDUR_EN)

    rmw_field(t, MT_DMA_DCR0(band), MT_DMA_DCR0_MAX_RX_LEN, 1536)
    # disable rx rate report by default (hw issue)
    clear_bits(t, MT_DMA_DCR0(band), MT_DMA_DCR0_RXD_G5_EN)

    # filter out non-resp frames + instantaneous signal reporting (RCPI mode 0, param 3)
    mask = MT_WTBLOFF_TOP_RSCR_RCPI_MODE | MT_WTBLOFF_TOP_RSCR_RCPI_PARAM
    set_ = (_field_prep(MT_WTBLOFF_TOP_RSCR_RCPI_MODE, 0)
            | _field_prep(MT_WTBLOFF_TOP_RSCR_RCPI_PARAM, 0x3))
    rmw(t, MT_WTBLOFF_TOP_RSCR(band), mask, set_)


def mac_init(t) -> None:
    """mt7921_mac_init — the register block only.

    The trailing mt76_connac_mcu_set_rts_thresh(0x92b, 0) is an MCU command, not
    a register write, so the bring-up orchestration sends it (mcu.set_rts_thresh)
    right after this returns — preserving the wire order.
    """
    rmw_field(t, MT_MDP_DCR1, MT_MDP_DCR1_MAX_RX_LEN, 1536)
    set_bits(t, MT_MDP_DCR0, MT_MDP_DCR0_DAMSDU_EN)        # hw de-agg
    set_bits(t, MT_MDP_DCR0, MT_MDP_DCR0_RX_HDR_TRANS_EN)  # hw rx hdr translation

    for i in range(MT792x_WTBL_SIZE):
        wtbl_update(t, i, MT_WTBL_UPDATE_ADM_COUNT_CLEAR)
    for band in range(2):
        mac_init_band(t, band)
