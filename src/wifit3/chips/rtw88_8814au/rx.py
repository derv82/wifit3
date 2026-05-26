"""RTL8814AU RX-side glue (M5) — monitor MAC init + frame iteration.

Reuses :mod:`wifit3.chips.rtw88_base.rx_common` for endpoint probing, the
24-byte rx_pkt_desc decode, and the burst frame iterator (the 8814a rx desc is
24 bytes, same as the 8822b). RSSI extraction (rtw8814a_query_phy_status, 4
paths) is a follow-up — frames flow with a placeholder RSSI for now.

`mac_init_for_rx` is the RX-relevant subset of rtw8814a_mac_init +
rtw_drv_info_cfg that was deferred from M2:
  - RXFLTMAP0/1/2     accept mgmt/ctrl/data subtypes
  - RX_DRVINFO_SZ     PHY_STATUS_SIZE (4) so phy_status rides each frame
  - rxdesc-len quirk  REG_TRXFF_BNDY+1 |= 0xF (mac.c:1378, 3081-only)
  - RCR               promiscuous monitor (RCR_MONITOR, incl. APP_PHYSTS)
  - WMAC_OPTION       clear bits 8|9 (rtw_drv_info_cfg)
  - USB burst         REG_RXDMA_MODE + REG_TXDMA_OFFSET_CHK drop-data

RX DMA itself is already enabled — REG_CR got MAC_TRX_ENABLE (0xFF, incl.
HCI_RXDMA/RXDMA/MACRXEN) back in M2's txdma_queue_mapping.
"""

from __future__ import annotations

import logging

from wifit3.chips.rtw88_base.rx_common import (  # noqa: F401 (re-exports)
    RX_PKT_DESC_SZ,
    Endpoints,
    RxPktStat,
    iter_bulk_frames as _shared_iter_bulk_frames,
    parse_rx_pkt_desc,
    probe_endpoints,
    read_rx_burst,
)

from . import constants as C
from .transport import RTL8814AUTransport

logger = logging.getLogger(__name__)


def mac_init_for_rx(transport: RTL8814AUTransport) -> None:
    """RX-relevant MAC init (deferred from M2): RX filters + drv_info + burst."""
    # RX filter maps (rtw8814a_mac_init).
    transport.write16(C.REG_RXFLTMAP0, C.RXFLTMAP0_8814A)
    transport.write16(C.REG_RXFLTMAP1, C.RXFLTMAP1_8814A)
    transport.write16(C.REG_RXFLTMAP2, C.RXFLTMAP2_8814A)

    # rtw_drv_info_cfg (mac.c:1373, 3081 path).
    transport.write8(C.REG_RX_DRVINFO_SZ, C.PHY_STATUS_SIZE)
    # "rxdesc len = 0" workaround: low nibble of REG_TRXFF_BNDY+1 = 0xF.
    v = (transport.read8(C.REG_TRXFF_BNDY + 1) & 0xF0) | 0x0F
    transport.write8(C.REG_TRXFF_BNDY + 1, v & 0xFF)
    # Promiscuous monitor RCR (includes APP_PHYSTS so phy_status rides frames).
    transport.write32(C.REG_RCR, C.RCR_MONITOR)
    transport.write32_clr(C.REG_WMAC_OPTION_FUNCTION + 4, (1 << 8) | (1 << 9))

    # USB RX burst (rtw_usb_init_burst_pkt_len) — HS uses BURST_SIZE_512.
    BIT_DMA_MODE = 1 << 1
    BIT_DMA_BURST_CNT = (1 << 2) | (1 << 3)
    BIT_DMA_BURST_SIZE_512 = 1
    rxdma = BIT_DMA_BURST_CNT | BIT_DMA_MODE
    rxdma |= (BIT_DMA_BURST_SIZE_512 << 4) & 0x30
    transport.write8(C.REG_RXDMA_MODE, rxdma & 0xFF)
    BIT_DROP_DATA_EN = 1 << 9
    transport.write16(C.REG_TXDMA_OFFSET_CHK,
                      transport.read16(C.REG_TXDMA_OFFSET_CHK) | BIT_DROP_DATA_EN)


def apply_monitor_rcr(transport: RTL8814AUTransport) -> None:
    """Force the promiscuous monitor RCR (also re-applied on warm reattach)."""
    transport.write32(C.REG_RCR, C.RCR_MONITOR)
    rcr = transport.read32(C.REG_RCR)
    logger.info("RX filter: RCR=0x%08x (AAP=%d)", rcr, 1 if rcr & 0x1 else 0)


def iter_bulk_frames(buf: bytes):
    """Yield (stat, mpdu, rssi) per frame. RSSI is a placeholder (None →
    parser uses -100) until rtw8814a_query_phy_status is ported."""
    return _shared_iter_bulk_frames(buf, phy_status_rssi=None)
