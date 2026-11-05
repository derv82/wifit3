"""RTL8188FTV DKMS MISC02 queue/page/filter init (M5d).

Ported from ``_InitQueueReservedPage`` / ``_InitTxBufferBoundary`` /
``_InitQueuePriority`` (+ ``_InitNormalChipTwoOutEpPriority`` /
``_InitNormalChipRegPriority``) / ``_InitPageBoundary`` /
``_InitTransferPageSize_8188fu`` / ``_InitDriverInfoSize`` /
``hal_init_macaddr`` (via ``hw_var_set_macaddr``) / ``_InitNetworkType`` /
``_InitWMACSetting`` / ``_InitAdaptiveCtrl`` / ``_InitEDCA`` /
``_InitRateFallback`` / ``_InitRetryFunction``
(hal/rtl8188f/usb/usb_halinit.c:242-330,421-460,481-560,
hal/rtl8188f/rtl8188f_hal_init.c:5117-5129, hal/hal_com.c:681-689).
``_InitInterrupt`` is empty (CONFIG_SUPPORT_USB_INT off);
``bRDGEnable`` never set; concurrent-mode blocks compile out.
Endpoint counts come from USB probing (2 bulk-OUT on this dongle);
``wifi_spec`` registry default is 0.
"""
from __future__ import annotations

from . import constants as C


def BIT(n: int) -> int:
    return 1 << n


TX_SELE_HQ = BIT(0)
TX_SELE_LQ = BIT(1)
TX_SELE_NQ = BIT(2)

RCR_ALL = (C.RCR_APM | C.RCR_AM | C.RCR_AB | C.RCR_CBSSID_DATA
           | C.RCR_CBSSID_BCN | C.RCR_APP_ICV | C.RCR_AMF
           | C.RCR_HTC_LOC_CTRL | C.RCR_APP_MIC | C.RCR_APP_PHYST_RXFF)


def init_queue_reserved_page(t, out_ep_queue_sel: int, wifi_spec: bool = False) -> None:
    num_hq = 0x0C if out_ep_queue_sel & TX_SELE_HQ else 0
    num_lq = 0x02 if out_ep_queue_sel & TX_SELE_LQ else 0
    num_nq = 0x02 if out_ep_queue_sel & TX_SELE_NQ else 0
    t.write8(C.REG_RQPN_NPQ, num_nq & 0xFF)
    num_pub = C.TX_TOTAL_PAGES - num_hq - num_lq - num_nq
    t.write32(C.REG_RQPN, num_hq | (num_lq << 8) | (num_pub << 16) | BIT(31))


def init_tx_buffer_boundary(t, wifi_spec: bool = False) -> None:
    bndy = C.TX_PAGE_BOUNDARY
    t.write8(C.REG_TXPKTBUF_BCNQ_BDNY, bndy)
    t.write8(C.REG_TXPKTBUF_MGQ_BDNY, bndy)
    t.write8(C.REG_TXPKTBUF_WMAC_LBK_BF_HD, bndy)
    t.write8(C.REG_TRXFF_BNDY, bndy)
    t.write8(C.REG_TDECTRL + 1, bndy)


def _reg_priority(t, be_q: int, bk_q: int, vi_q: int, vo_q: int,
                  mgt_q: int, hi_q: int) -> None:
    value16 = t.read16(C.REG_TRXDMA_CTRL) & 0x7
    value16 |= ((be_q & 0x3) << 8) | ((bk_q & 0x3) << 10) | ((vi_q & 0x3) << 6) \
        | ((vo_q & 0x3) << 4) | ((mgt_q & 0x3) << 12) | ((hi_q & 0x3) << 14)
    t.write16(C.REG_TRXDMA_CTRL, value16)


def init_queue_priority(t, out_ep_number: int, out_ep_queue_sel: int,
                        wifi_spec: bool = False) -> None:
    if out_ep_number == 2:
        if out_ep_queue_sel == TX_SELE_HQ | TX_SELE_NQ:
            hi, low = C.QUEUE_HIGH, C.QUEUE_NORMAL
        elif out_ep_queue_sel == TX_SELE_HQ | TX_SELE_LQ:
            hi, low = C.QUEUE_HIGH, C.QUEUE_LOW
        elif out_ep_queue_sel == TX_SELE_NQ | TX_SELE_LQ:
            hi, low = C.QUEUE_NORMAL, C.QUEUE_LOW
        else:
            hi, low = 0, 0
        if not wifi_spec:
            _reg_priority(t, low, low, hi, hi, hi, hi)
        else:
            _reg_priority(t, low, hi, hi, low, hi, hi)
    elif out_ep_number in (3, 4):
        # TODO: verify, untested here, needs a 3-4 bulk-OUT 8188F dongle
        if not wifi_spec:
            _reg_priority(t, C.QUEUE_LOW, C.QUEUE_LOW, C.QUEUE_NORMAL,
                          C.QUEUE_HIGH, C.QUEUE_HIGH, C.QUEUE_HIGH)
        else:
            _reg_priority(t, C.QUEUE_LOW, C.QUEUE_NORMAL, C.QUEUE_NORMAL,
                          C.QUEUE_HIGH, C.QUEUE_HIGH, C.QUEUE_HIGH)


def init_page_boundary(t) -> None:
    t.write16(C.REG_TRXFF_BNDY + 2, C.RX_DMA_BOUNDARY)


def init_transfer_page_size(t) -> None:
    t.write8(C.REG_PBP, 0x2 | (0x2 << 4))


def init_driver_info_size(t) -> None:
    t.write8(C.REG_RX_DRVINFO_SZ, C.DRVINFO_SZ)


def init_macaddr(t, mac: bytes) -> None:
    for idx in range(6):
        t.write8(C.REG_MACID + idx, mac[idx])


def init_network_type(t) -> None:
    value32 = t.read32(C.REG_CR)
    t.write32(C.REG_CR, (value32 & ~C.MASK_NETTYPE) | ((C.NETTYPE_AP & 0x3) << 16))


def init_wmac_setting(t) -> None:
    t.write32(C.REG_RCR, RCR_ALL)
    t.write16(C.REG_RXFLTMAP2, 0xFFFF)
    t.write16(C.REG_RXFLTMAP1, 0x0400)
    t.write16(C.REG_RXFLTMAP0, 0xFFFF)


def init_adaptive_ctrl(t) -> None:
    value32 = t.read32(C.REG_RRSR)
    t.write32(C.REG_RRSR, (value32 & ~C.RATE_BITMAP_ALL) | C.RATE_RRSR_CCK_ONLY_1M)
    t.write16(C.REG_SPEC_SIFS, 0x1010)
    t.write16(C.REG_RL, 0x3030)


def init_edca(t) -> None:
    t.write16(C.REG_SPEC_SIFS, 0x100A)
    t.write16(C.REG_MAC_SPEC_SIFS, 0x100A)
    t.write16(C.REG_SIFS_CTX, 0x100A)
    t.write16(C.REG_SIFS_TRX, 0x100A)
    t.write32(C.REG_EDCA_BE_PARAM, 0x005EA42B)
    t.write32(C.REG_EDCA_BK_PARAM, 0x0000A44F)
    t.write32(C.REG_EDCA_VI_PARAM, 0x005EA324)
    t.write32(C.REG_EDCA_VO_PARAM, 0x002FA226)


def init_rate_fallback(t) -> None:
    t.write32(C.REG_DARFRC, 0x00000000)
    t.write32(C.REG_DARFRC + 4, 0x10080404)
    t.write32(C.REG_RARFRC, 0x04030201)
    t.write32(C.REG_RARFRC + 4, 0x08070605)


def init_retry_function(t) -> None:
    value8 = t.read8(C.REG_FWHW_TXQ_CTRL)
    t.write8(C.REG_FWHW_TXQ_CTRL, value8 | C.EN_AMPDU_RTY_NEW)
    t.write8(C.REG_ACKTO, 0x40)
