"""RTL8188EUS MAC register configuration (M2a).

``PHY_MACConfig8188E`` [SRC] rtl8188e_phycfg.c:758 loads the MAC register table
through the phydm walker (``odm_config_mac_8188e`` = 8-bit writes), then sets the
AMPDU aggregation number. [WIRE] cap1 ops 797..end-of-table.
"""
from __future__ import annotations

from . import phy_cond
from .constants import (
    _LLT_NO_ACTIVE,
    _LLT_WRITE_ACCESS,
    LAST_ENTRY_OF_TX_PKT_BUFFER,
    REG_BCNQ_BDNY,
    REG_LLT_INIT,
    REG_MAX_AGGR_NUM,
    REG_MGQ_BDNY,
    REG_TDECTRL,
    REG_TRXFF_BNDY,
    REG_WMAC_LBK_BF_HD,
    TX_PAGE_BOUNDARY,
)
from .mac_reg_tbl import MAC_REG

MAX_AGGR_NUM = 0x07  # [SRC] include/Hal8188EPhyCfg.h (USB build; 0x0B is PCI-only)
_LLT_POLL_CAP = 1000


def phy_mac_config(t) -> None:
    """Apply ``array_mp_8188e_mac_reg`` (each taken row is an 8-bit write), then the
    AMPDU aggregation number to REG_MAX_AGGR_NUM."""
    phy_cond.walk_table(MAC_REG, lambda addr, val: t.write8(addr, val & 0xFF))
    val = (MAX_AGGR_NUM << 8) | MAX_AGGR_NUM
    t.write16(REG_MAX_AGGR_NUM, val)


def init_tx_buffer_boundary(t, bndy: int = TX_PAGE_BOUNDARY) -> None:
    """``_InitTxBufferBoundary`` [SRC] usb/usb_halinit.c — program the TX page
    boundary into the beacon/mgmt/loopback/RXFF/TDECTRL boundary registers."""
    t.write8(REG_BCNQ_BDNY, bndy)
    t.write8(REG_MGQ_BDNY, bndy)
    t.write8(REG_WMAC_LBK_BF_HD, bndy)
    t.write8(REG_TRXFF_BNDY, bndy)
    t.write8(REG_TDECTRL + 1, bndy)


def _llt_write(t, address: int, data: int) -> None:
    """``_LLTWrite`` [SRC] rtl8188e_hal_init.c:2815 — one LLT entry, poll to idle."""
    value = (_LLT_WRITE_ACCESS << 30) | ((address & 0xFF) << 8) | (data & 0xFF)
    t.write32(REG_LLT_INIT, value)
    for _ in range(_LLT_POLL_CAP):
        if ((t.read32(REG_LLT_INIT) >> 30) & 0x3) == _LLT_NO_ACTIVE:
            return
    raise RuntimeError("RTL8188EUS: LLT write timeout")


def init_llt(t, bndy: int = TX_PAGE_BOUNDARY,
             last: int = LAST_ENTRY_OF_TX_PKT_BUFFER) -> None:
    """``InitLLTTable`` (direct, non-IOL: this build does not define CONFIG_IOL_LLT)
    [SRC] rtl8188e_hal_init.c:2860 — chain the TX page link-list, ring the rest."""
    for i in range(bndy - 1):
        _llt_write(t, i, i + 1)
    _llt_write(t, bndy - 1, 0xFF)              # end of list
    for i in range(bndy, last):
        _llt_write(t, i, i + 1)
    _llt_write(t, last, bndy)                  # ring buffer: last -> boundary
