"""RTL8821AU MAC power-on flow + post-FW MAC init helpers.

Direct port of `rtw_mac_power_on` (mac.c:378), its pre-FW helpers, and the
post-FW MAC-only block of `rtw88xxa_power_on` (rtw88xxa.c:1083..1175).

Reference (rtw88-source-v6.18):
    mac.c:62      rtw_mac_pre_system_cfg
    mac.c:272     rtw_mac_power_switch
    mac.c:355     __rtw_mac_init_system_cfg_legacy
    mac.c:378     rtw_mac_power_on
    rtw88xxa.c:370   rtw88xxa_llt_write
    rtw88xxa.c:391   rtw88xxa_llt_init
    rtw88xxa.c:418   rtw88xxau_init_queue_reserved_page
    rtw88xxa.c:455   rtw88xxau_init_tx_buffer_boundary
    rtw88xxa.c:466   rtw88xxau_init_queue_priority
    rtw88xxa.c:512   rtw88xxa_init_wmac_setting
    rtw88xxa.c:522   rtw88xxa_init_adaptive_ctrl
    rtw88xxa.c:528   rtw88xxa_init_edca
    rtw88xxa.c:545   rtw88xxau_tx_aggregation
    rtw88xxa.c:557   rtw88xxa_init_beacon_parameters
    usb.c:846        rtw_usb_init_burst_pkt_len
"""

from __future__ import annotations

import logging
import time

from .constants import (
    BIT_AAP,
    BIT_CBSSID_DATA,
    RCR_MONITOR,
    BIT_DIS_TSF_UDT,
    BIT_APP_PHYSTS,
    BIT_EN_BCN_FUNCTION,
    BIT_DMA_BURST_CNT,
    BIT_DMA_BURST_SIZE_512,
    BIT_DMA_MODE,
    BIT_DROP_DATA_EN,
    BIT_EN_SIC,
    BIT_EN_SINGLE_APMDU,
    BIT_LD_RQPN,
    BIT_LDO,
    BIT_LLT_WRITE_ACCESS,
    BIT_MACRXEN,
    BIT_MACTXEN,
    BIT_MASK_DMA_BURST_SIZE,
    BIT_MASK_TXDMA_MAP,
    BIT_SHIFT_DMA_BURST_SIZE,
    BIT_SHIFT_TXDMA_BEQ_MAP,
    BIT_SHIFT_TXDMA_BKQ_MAP,
    BIT_SHIFT_TXDMA_HIQ_MAP,
    BIT_SHIFT_TXDMA_MGQ_MAP,
    BIT_SHIFT_TXDMA_VIQ_MAP,
    BIT_SHIFT_TXDMA_VOQ_MAP,
    BIT_WAKEPAD_EN,
    LDO_SEL,
    PG_TBL_USB2_EXQ_NUM,
    PG_TBL_USB2_GAPQ_NUM,
    PG_TBL_USB2_HQ_NUM,
    PG_TBL_USB2_LQ_NUM,
    PG_TBL_USB2_NQ_NUM,
    PHY_STATUS_SIZE,
    REG_ACKTO,
    REG_AMPDU_MAX_LENGTH,
    REG_AMPDU_MAX_TIME,
    REG_ARFR0,
    REG_ARFR1_V1,
    REG_ARFR2_V1,
    REG_ARFR3_V1,
    REG_ARFRH0,
    REG_ARFRH1_V1,
    REG_ARFRH2_V1,
    REG_ARFRH3_V1,
    REG_BCN_CTRL,
    REG_BCN_MAX_ERR,
    REG_BCNDMATIM,
    REG_BCNQ_BDNY,
    REG_BCNTCFG,
    REG_CR,
    REG_CR_OFF_VALUE,
    REG_DRVERLYINT,
    REG_DWBCN0_CTRL,
    REG_DWBCN1_CTRL,
    REG_EDCA_BE_PARAM,
    REG_EDCA_BK_PARAM,
    REG_EDCA_VI_PARAM,
    REG_EDCA_VO_PARAM,
    REG_FAST_EDCA_CTRL,
    REG_FWHW_TXQ_CTRL,
    REG_GPIO_MUXCFG,
    REG_HIMR0,
    REG_HIMR1,
    REG_HMETFR,
    REG_HWSEQ_CTRL,
    REG_LDO_SWR_CTRL,
    REG_LLT_INIT,
    REG_MAC_SPEC_SIFS,
    REG_MAR,
    REG_MAX_AGGR_NUM,
    REG_MGQ_BDNY,
    REG_PIFS,
    REG_RETRY_LIMIT,
    REG_RQPN,
    REG_RQPN_NPQ,
    REG_RRSR,
    REG_RSV_CTRL,
    REG_RX_DRVINFO_SZ,
    REG_RX_PKT_LIMIT,
    REG_RXDMA_MODE,
    REG_RXDMA_STATUS,
    REG_RCR,
    REG_RXFLTMAP0,
    REG_RXFLTMAP1,
    REG_RXFLTMAP2,
    REG_SIFS,
    REG_SINGLE_AMPDU_CTRL,
    REG_SPEC_SIFS,
    REG_SYS_CFG1,
    REG_SYS_CLKR,
    REG_TBTT_PROHIBIT,
    REG_TRXFF_BNDY,
    REG_TXDMA_OFFSET_CHK,
    REG_TXDMA_PQ_MAP,
    REG_USB3_RXITV,
    REG_USTIME_EDCA,
    REG_USTIME_TSF,
    REG_WMAC_LBK_BF_HD,
    REG_WMAC_OPTION_FUNCTION,
    REPORT_BUF,
    RQPN_USB2_BE,
    RQPN_USB2_BK,
    RQPN_USB2_HI,
    RQPN_USB2_MG,
    RQPN_USB2_VI,
    RQPN_USB2_VO,
    RXFF_SIZE,
    SPS_SEL,
    USB_TX_AGG_DESC_NUM,
    WLAN_TBTT_TIME,
)
from .fifo import FifoConf, set_trx_fifo_info
from .power_seq import (
    CARD_DISABLE_FLOW_8821A,
    CARD_ENABLE_FLOW_8821A,
    INTF_USB,
    run_pwr_seq,
)
from .transport import RTL8821AUTransport

# REG_SYS_STATUS1+1 BIT(0) is the USB-only secondary "is the chip off?" tell
# (mac.c:293). When set, the chip considers itself powered down even if
# REG_CR is non-0xEA.
REG_SYS_STATUS1 = 0x00F4
BIT_USB_PWR_OFF = 1 << 0  # at REG_SYS_STATUS1+1

logger = logging.getLogger(__name__)


def mac_pre_system_cfg(transport: RTL8821AUTransport) -> None:
    """USB-on-8051 path of `rtw_mac_pre_system_cfg` (mac.c:62).

    For 8821A the function:
        1. clears REG_RSV_CTRL
        2. picks LDO_SEL vs SPS_SEL based on BIT_LDO of REG_SYS_CFG1
        3. returns (USB path doesn't fall through to the 8814A-style writes)
    """
    transport.write8(REG_RSV_CTRL, 0)
    sys_cfg1 = transport.read32(REG_SYS_CFG1)
    if sys_cfg1 & BIT_LDO:
        logger.debug("BIT_LDO set in REG_SYS_CFG1=0x%08x -> LDO_SEL", sys_cfg1)
        transport.write8(REG_LDO_SWR_CTRL, LDO_SEL)
    else:
        logger.debug("BIT_LDO clear in REG_SYS_CFG1=0x%08x -> SPS_SEL", sys_cfg1)
        transport.write8(REG_LDO_SWR_CTRL, SPS_SEL)


def _is_chip_powered(transport: RTL8821AUTransport) -> bool:
    """Mirror the USB+8051 branch of mac.c:291.

    The chip is OFF when either:
      * REG_CR == 0xEA  (kernel's primary "off" sentinel), or
      * REG_SYS_STATUS1+1 has BIT(0) set (USB-only secondary tell).
    """
    if transport.read8(REG_CR) == REG_CR_OFF_VALUE:
        return False
    if transport.read8(REG_SYS_STATUS1 + 1) & BIT_USB_PWR_OFF:
        return False
    return True


def mac_power_switch(transport: RTL8821AUTransport, pwr_on: bool) -> bool:
    """USB+8051 path of `rtw_mac_power_switch` (mac.c:272).

    Returns:
        True  if pwr_seq ran successfully (state changed)
        False if the device was already in the requested state (`-EALREADY`)
    """
    cur_pwr = _is_chip_powered(transport)
    if pwr_on == cur_pwr:
        logger.debug("mac_power_switch: already %s", "on" if pwr_on else "off")
        return False

    pwr_seq = CARD_ENABLE_FLOW_8821A if pwr_on else CARD_DISABLE_FLOW_8821A
    for sub in pwr_seq:
        run_pwr_seq(transport, sub, intf_mask=INTF_USB)
    return True


def mac_init_system_cfg_legacy(transport: RTL8821AUTransport) -> None:
    """Port of `__rtw_mac_init_system_cfg_legacy` (mac.c:355)."""
    transport.write8(REG_CR, 0xFF)
    time.sleep(0.002)
    transport.write8(REG_HWSEQ_CTRL, 0x7F)
    time.sleep(0.002)
    transport.write8_set(REG_SYS_CLKR, BIT_WAKEPAD_EN)
    # write16_clr on REG_GPIO_MUXCFG; we don't have a write16_clr helper, do
    # it explicitly to match `rtw_write16_clr(REG_GPIO_MUXCFG, BIT_EN_SIC)`.
    cur = transport.read16(REG_GPIO_MUXCFG)
    transport.write16(REG_GPIO_MUXCFG, cur & ((~BIT_EN_SIC) & 0xFFFF))
    transport.write16(REG_CR, 0x02FF)


def mac_power_on(transport: RTL8821AUTransport) -> None:
    """Cold-plug MAC power-on flow.

    Mirrors the cold-plug branch of `rtw_mac_power_on` (mac.c:378):
        1. mac_pre_system_cfg
        2. mac_power_switch(True) — card_enable_flow_8821a
        3. mac_init_system_cfg_legacy

    Raises if the chip reports it is already powered. Callers that want
    to handle a warm device should detect that BEFORE invoking this:
    typically via :func:`is_chip_warm` from this module, and then skip
    the bring-up entirely and just reattach to the running session.
    The kernel's power-cycle-on-EALREADY path is intentionally *not*
    mirrored here — empirically it leaves the bulk-IN pipe in a state
    where RX is dead until a fresh USB re-enumeration.
    """
    mac_pre_system_cfg(transport)
    changed = mac_power_switch(transport, True)
    if not changed:
        raise IOError(
            "mac_power_on: chip already powered. The caller should detect "
            "this with is_chip_warm() and skip the bring-up instead."
        )
    mac_init_system_cfg_legacy(transport)


def is_chip_warm(transport: RTL8821AUTransport) -> bool:
    """Return True if the chip looks like it has FW running + MAC enabled.

    Signal: REG_MCUFW_CTRL has all four FW_READY_LEGACY bits set AND the
    MAC has its TX/RX engines enabled (REG_CR bits 6+7). This is the
    state the chip is in after a successful :func:`mac_power_on` →
    FW upload + validate → post_fw_mac_init chain — i.e. exactly what a
    previous wifit3 session would leave behind.
    """
    from .constants import (
        BIT_MACRXEN,
        BIT_MACTXEN,
        FW_READY_LEGACY,
        REG_MCUFW_CTRL,
    )
    try:
        mcufw = transport.read32(REG_MCUFW_CTRL)
        cr = transport.read8(REG_CR)
    except IOError:
        return False
    fw_running = (mcufw & FW_READY_LEGACY) == FW_READY_LEGACY
    mac_enabled = (cr & (BIT_MACTXEN | BIT_MACRXEN)) == (BIT_MACTXEN | BIT_MACRXEN)
    return fw_running and mac_enabled


# ---------------------------------------------------------------------------
# LLT — Look-up Table for the TX FIFO page ring (rtw88xxa.c:370,391)
# ---------------------------------------------------------------------------

_LLT_POLL_MAX = 21    # kernel loops while count <= 20


def llt_write(transport: RTL8821AUTransport, address: int, data: int) -> None:
    """Write one LLT entry, polling for completion.

    Mirrors `rtw88xxa_llt_write` (rtw88xxa.c:370). The 32-bit REG_LLT_INIT
    encodes:
        bits[31:30] = BIT_LLT_WRITE_ACCESS  (set during a write request)
        bits[23:16] = address (LLT page index, 0..255)
        bits[7:0]   = data    (next-page link value)
    The hardware clears the top two bits once the write commits.
    """
    value = BIT_LLT_WRITE_ACCESS | ((address & 0xFF) << 8) | (data & 0xFF)
    transport.write32(REG_LLT_INIT, value)
    for _ in range(_LLT_POLL_MAX):
        if not (transport.read32(REG_LLT_INIT) & (3 << 30)):
            return
    raise IOError(f"LLT write to entry {address} failed to complete (poll timeout)")


def llt_init(transport: RTL8821AUTransport, boundary: int) -> None:
    """Initialise the LLT page-link ring.

    Mirrors `rtw88xxa_llt_init` (rtw88xxa.c:391). For boundary=248 (8821A):
        * entries 0..246 → i+1                  (chain forward)
        * entry 247      → 0xFF                 (mark end of TX-FIFO half)
        * entries 248..254 → i+1                (chain forward in RSVD half)
        * entry 255      → 248 (=boundary)      (wrap RSVD half to its head)
    That gives 256 total LLT writes.
    """
    last_entry = 255
    for i in range(boundary - 1):
        llt_write(transport, i, i + 1)
    llt_write(transport, boundary - 1, 0xFF)
    for i in range(boundary, last_entry):
        llt_write(transport, i, i + 1)
    llt_write(transport, last_entry, boundary)


# ---------------------------------------------------------------------------
# Pre-FW init that we previously skipped (rtw88xxa.c:1055..1067 minus FW load)
# ---------------------------------------------------------------------------

def pre_fw_init(transport: RTL8821AUTransport) -> FifoConf:
    """Mirrors rtw88xxa.c:1055..1067.

    Runs after `mac_power_on` and before FW upload. Returns the FifoConf
    used by the post-FW queue setup.
    """
    fifo = set_trx_fifo_info()
    llt_init(transport, fifo.rsvd_boundary)
    transport.write32_set(REG_TXDMA_OFFSET_CHK, BIT_DROP_DATA_EN)
    return fifo


# ---------------------------------------------------------------------------
# Post-FW MAC init helpers (rtw88xxa.c:418..569)
# ---------------------------------------------------------------------------

def init_queue_reserved_page(transport: RTL8821AUTransport, fifo: FifoConf) -> None:
    """Mirrors `rtw88xxau_init_queue_reserved_page` (rtw88xxa.c:418).

    For USB-2-bulkout 8821A using `page_table[2]`:
        hq=8, nq=0, lq=0, exq=0, gapq=1
        pubq = acq_pg_num - hq - lq - nq - exq - gapq = 248 - 10 = 238
    """
    hq = PG_TBL_USB2_HQ_NUM
    nq = PG_TBL_USB2_NQ_NUM
    lq = PG_TBL_USB2_LQ_NUM
    exq = PG_TBL_USB2_EXQ_NUM
    gapq = PG_TBL_USB2_GAPQ_NUM
    pubq = fifo.acq_pg_num - hq - lq - nq - exq - gapq

    # BIT_RQPN_NE(n, e) = (n << 0) | (e << 16)
    val_npq = (nq & 0xFF) | ((exq & 0xFF) << 16)
    transport.write32(REG_RQPN_NPQ, val_npq)

    # BIT_RQPN_HLP(h, l, p) = BIT_LD_RQPN | (h << 0) | (l << 8) | (p << 16)
    val_rqpn = (
        BIT_LD_RQPN
        | (hq & 0xFF)
        | ((lq & 0xFF) << 8)
        | ((pubq & 0xFF) << 16)
    )
    transport.write32(REG_RQPN, val_rqpn)


def init_tx_buffer_boundary(transport: RTL8821AUTransport, fifo: FifoConf) -> None:
    """Mirrors `rtw88xxau_init_tx_buffer_boundary` (rtw88xxa.c:455)."""
    b = fifo.rsvd_boundary & 0xFF
    transport.write8(REG_BCNQ_BDNY, b)
    transport.write8(REG_MGQ_BDNY, b)
    transport.write8(REG_WMAC_LBK_BF_HD, b)
    transport.write8(REG_TRXFF_BNDY, b)
    transport.write8(REG_DWBCN0_CTRL + 1, b)


def init_queue_priority(transport: RTL8821AUTransport) -> None:
    """Mirrors `rtw88xxau_init_queue_priority` (rtw88xxa.c:466) for USB-2-bulkout.

    Reads REG_TXDMA_PQ_MAP (keeping low 3 bits), OR's in the lane mappings
    from rqpn_table[2] = {NORMAL, NORMAL, LOW, LOW, EXTRA, HIGH}, and
    writes the 16-bit value back.
    """
    txdma_pq_map = transport.read16(REG_TXDMA_PQ_MAP) & 0x7
    mappings = (
        (RQPN_USB2_HI, BIT_SHIFT_TXDMA_HIQ_MAP),
        (RQPN_USB2_MG, BIT_SHIFT_TXDMA_MGQ_MAP),
        (RQPN_USB2_BK, BIT_SHIFT_TXDMA_BKQ_MAP),
        (RQPN_USB2_BE, BIT_SHIFT_TXDMA_BEQ_MAP),
        (RQPN_USB2_VI, BIT_SHIFT_TXDMA_VIQ_MAP),
        (RQPN_USB2_VO, BIT_SHIFT_TXDMA_VOQ_MAP),
    )
    for value, shift in mappings:
        txdma_pq_map |= (value & BIT_MASK_TXDMA_MAP) << shift
    transport.write16(REG_TXDMA_PQ_MAP, txdma_pq_map)
    # bulkout_num != 4 for AWUS036ACS, so the HIQ_NO_LMT_EN poke is skipped.


def init_wmac_setting(transport: RTL8821AUTransport) -> None:
    """Mirrors `rtw88xxa_init_wmac_setting` (rtw88xxa.c:512)."""
    transport.write16(REG_RXFLTMAP0, 0xFFFF)
    transport.write16(REG_RXFLTMAP1, 0x0400)
    transport.write16(REG_RXFLTMAP2, 0xFFFF)
    transport.write32(REG_MAR, 0xFFFFFFFF)
    transport.write32(REG_MAR + 4, 0xFFFFFFFF)


# RXFLTMAP1 gates ctrl subtypes; init_wmac_setting leaves it at 0x0400 (ps-poll only).
# Bit 13 = ACK, opened on demand so monitor RX can see the AP's ACK to our injects.
RXFLTMAP1_ACK = 1 << 13


def admit_ack_frames(transport: RTL8821AUTransport) -> None:
    """RXFLTMAP1 |= BIT(13) — let RX see the AP's ACKs to our injects. Off by default."""
    transport.write16(REG_RXFLTMAP1, transport.read16(REG_RXFLTMAP1) | RXFLTMAP1_ACK)


def drop_ack_frames(transport: RTL8821AUTransport) -> None:
    """Clear RXFLTMAP1 BIT(13) — restore the default monitor ctrl filter."""
    transport.write16(REG_RXFLTMAP1, transport.read16(REG_RXFLTMAP1) & ~RXFLTMAP1_ACK)


def init_adaptive_ctrl(transport: RTL8821AUTransport) -> None:
    """Mirrors `rtw88xxa_init_adaptive_ctrl` (rtw88xxa.c:522)."""
    transport.write32_mask(REG_RRSR, 0xFFFFF, 0xFFFF1)
    transport.write16(REG_RETRY_LIMIT, 0x3030)


def init_edca(transport: RTL8821AUTransport) -> None:
    """Mirrors `rtw88xxa_init_edca` (rtw88xxa.c:528)."""
    transport.write16(REG_SPEC_SIFS, 0x100A)
    transport.write16(REG_MAC_SPEC_SIFS, 0x100A)
    transport.write16(REG_SIFS, 0x100A)
    transport.write16(REG_SIFS + 2, 0x100A)
    transport.write32(REG_EDCA_BE_PARAM, 0x005EA42B)
    transport.write32(REG_EDCA_BK_PARAM, 0x0000A44F)
    transport.write32(REG_EDCA_VI_PARAM, 0x005EA324)
    transport.write32(REG_EDCA_VO_PARAM, 0x002FA226)
    transport.write8(REG_USTIME_TSF, 0x50)
    transport.write8(REG_USTIME_EDCA, 0x50)


def init_beacon_parameters(transport: RTL8821AUTransport, *, btcoex: bool = False) -> None:
    """Mirrors `rtw88xxa_init_beacon_parameters` (rtw88xxa.c:557).

    Without EFUSE we assume btcoex=False (will be plumbed in M4c).
    """
    val16 = (BIT_DIS_TSF_UDT << 8) | BIT_DIS_TSF_UDT
    if btcoex:
        val16 |= BIT_EN_BCN_FUNCTION
    transport.write16(REG_BCN_CTRL, val16)
    transport.write32_mask(REG_TBTT_PROHIBIT, 0xFFFFF, WLAN_TBTT_TIME)
    transport.write8(REG_DRVERLYINT, 0x05)
    transport.write8(REG_BCNDMATIM, 0x02)  # WLAN_BCN_DMA_TIME
    transport.write16(REG_BCNTCFG, 0x4413)


def tx_aggregation(transport: RTL8821AUTransport) -> None:
    """Mirrors `rtw88xxau_tx_aggregation` (rtw88xxa.c:545)."""
    transport.write32_mask(REG_DWBCN0_CTRL, 0xF0, USB_TX_AGG_DESC_NUM)
    # 8821A only:
    transport.write8(REG_DWBCN1_CTRL, USB_TX_AGG_DESC_NUM << 1)


def usb_interface_cfg(transport: RTL8821AUTransport) -> None:
    """Mirrors `rtw_usb_interface_cfg` → `rtw_usb_init_burst_pkt_len` (usb.c:846).

    AWUS036ACS is USB 2.0 high-speed → BIT_DMA_BURST_SIZE_512.
    Also sets BIT_DROP_DATA_EN in REG_TXDMA_OFFSET_CHK (already set in
    pre_fw_init; the kernel re-sets it here defensively).
    """
    rxdma = BIT_DMA_BURST_CNT | BIT_DMA_MODE
    rxdma &= ~BIT_MASK_DMA_BURST_SIZE
    rxdma |= (BIT_DMA_BURST_SIZE_512 << BIT_SHIFT_DMA_BURST_SIZE) & BIT_MASK_DMA_BURST_SIZE
    transport.write8(REG_RXDMA_MODE, rxdma)
    cur = transport.read16(REG_TXDMA_OFFSET_CHK)
    transport.write16(REG_TXDMA_OFFSET_CHK, cur | (BIT_DROP_DATA_EN & 0xFFFF))


# ---------------------------------------------------------------------------
# The orchestrator: full post-FW MAC-only init (rtw88xxa.c:1083..1175)
# ---------------------------------------------------------------------------

def post_fw_mac_init(transport: RTL8821AUTransport, fifo: FifoConf) -> None:
    """Run the MAC-only chunk of rtw88xxa_power_on after FW is running.

    Covers lines 1083..1175 (inclusive of MACTXEN|MACRXEN set), stopping
    BEFORE phy_bb_config / phy_rf_config / switch_band / rtw_phy_init —
    those are M4c.

    Pre-condition: `rtw88xxa_llt_init` already ran (via :func:`pre_fw_init`),
    and FW is running (M3 validate passed).
    """
    # rtw88xxa.c:1081
    transport.write8(REG_HMETFR, 0x0F)
    # rtw88xxa.c:1083 — kernel does rtw_load_table(chip->mac_tbl) here. We
    # defer it to M4c so this milestone stays a tight register-write set.
    # If the rest of M4b needs it earlier than expected we'll reorder.

    init_queue_reserved_page(transport, fifo)
    init_tx_buffer_boundary(transport, fifo)
    init_queue_priority(transport)

    # 1089: REG_TRXFF_BNDY + 2 (u16)
    transport.write16(REG_TRXFF_BNDY + 2, RXFF_SIZE - REPORT_BUF - 1)

    # 1097 — rtw_drv_info_cfg (mac.c:1373). Sets PHY_STATUS_SIZE, enables
    # BIT_APP_PHYSTS so the chip appends a Jaguar phy_status report to every
    # RX frame's drv_info area (needed for RSSI), and clears the two PHYSTS
    # option bits at REG_WMAC_OPTION_FUNCTION+4 per mac.c:1386.
    transport.write8(REG_RX_DRVINFO_SZ, PHY_STATUS_SIZE)
    transport.write32_set(REG_RCR, BIT_APP_PHYSTS)
    transport.write32_clr(REG_WMAC_OPTION_FUNCTION + 4, (1 << 8) | (1 << 9))

    # 1099-1100
    transport.write32(REG_HIMR0, 0)
    transport.write32(REG_HIMR1, 0)

    # 1102 — REG_CR mask 0x30000 = 0x2 (set bit 17)
    transport.write32_mask(REG_CR, 0x30000, 0x2)

    init_wmac_setting(transport)
    init_adaptive_ctrl(transport)
    init_edca(transport)

    # 1108 — REG_FWHW_TXQ_CTRL set BIT(7)
    transport.write8_set(REG_FWHW_TXQ_CTRL, 1 << 7)
    # 1109
    transport.write8(REG_ACKTO, 0x80)

    tx_aggregation(transport)
    init_beacon_parameters(transport)

    # 1114
    transport.write8(REG_BCN_MAX_ERR, 0xFF)

    # 1116
    usb_interface_cfg(transport)

    # 1119 — usb3 rx interval (kernel sets even for USB2, no harm)
    transport.write8(REG_USB3_RXITV, 0x01)

    # 1122
    transport.write16(REG_RXDMA_STATUS, 0x7400)
    transport.write8(REG_RXDMA_STATUS + 1, 0xF5)

    # 1126 — 8821A path
    transport.write8(REG_AMPDU_MAX_TIME, 0x5E)
    transport.write32(REG_AMPDU_MAX_LENGTH, 0xFFFFFFFF)
    transport.write8(REG_USTIME_TSF, 0x50)
    transport.write8(REG_USTIME_EDCA, 0x50)

    # 1135 — only for USB 3.0 SuperSpeed; AWUS036ACS is high-speed → skip
    # REG_USB_MOD BIT(3)|BIT(4) clear

    # 1139
    transport.write8_set(REG_SINGLE_AMPDU_CTRL, BIT_EN_SINGLE_APMDU)
    # 1142
    transport.write8(REG_RX_PKT_LIMIT, 0x18)
    # 1144
    transport.write8(REG_PIFS, 0x00)

    # 1146..1153 — 8821A branch
    transport.write16(REG_MAX_AGGR_NUM, 0x1F1F)
    transport.write8(REG_FWHW_TXQ_CTRL, 0x80)
    transport.write32(REG_FAST_EDCA_CTRL, 0x03087777)

    # 1157
    transport.write8_set(REG_RSV_CTRL, (1 << 5) | (1 << 6))

    # 1160-1173 — ARFB tables
    transport.write32(REG_ARFR0, 0x00000010)
    transport.write32(REG_ARFRH0, 0xFFFFF000)
    transport.write32(REG_ARFR1_V1, 0x00000010)
    transport.write32(REG_ARFRH1_V1, 0x003FF000)
    transport.write32(REG_ARFR2_V1, 0x00000015)
    transport.write32(REG_ARFRH2_V1, 0x003FF000)
    transport.write32(REG_ARFR3_V1, 0x00000015)
    transport.write32(REG_ARFRH3_V1, 0xFFCFF000)

    # 1175 — MACTXEN | MACRXEN
    transport.write8_set(REG_CR, BIT_MACTXEN | BIT_MACRXEN)


def apply_monitor_rx_filter(transport: RTL8821AUTransport) -> None:
    """Force the monitor RX filter. Called on BOTH cold init and warm reattach
    (from driver._finish_attach), because:

    - The kernel init leaves REG_RCR configured for an associated STA: not
      promiscuous, and checking BSSID on data frames. That dropped client→AP
      (ToDS) traffic — incl. the M2/M4 EAPOL a 4-way needs — so we only ever
      saw M1/M3 (AP→client). mac80211 sets monitor's RCR via
      ops->configure_filter, which wifit3 never gets.
    - A WARM chip inherited from a previous session (kernel STA driver, or an
      old wifit3 build) likewise has a non-monitor RCR, and the warm path skips
      post-FW init — so applying the filter here is what makes it take effect.

    Match what airmon-ng writes on the wire verbatim: REG_RCR = 0xf410400f —
    AAP (promiscuous) + APM/AM/AB, CBSSID checks cleared, APP_PHYSTS/ICV/MIC/FCS
    framing bits as the kernel sets them. (Net-type stays MGD_LINKED — [WIRE]
    frame 5265 shows the kernel leaves it at 2 in monitor; net-type is not the
    gate.) [SRC rtw88 reg.h:502-534; WIRE captures_rtw88_8821au frames 6679-6693]
    """
    transport.write32(REG_RCR, RCR_MONITOR)

    # Read back so a WIFIT3_LOG run shows the actual runtime RX-filter state vs
    # the airmon targets — confirms the write landed. If RCR reads back with AAP
    # set but ToDS frames still don't arrive, the gate is NOT the RX filter.
    rcr = transport.read32(REG_RCR)
    logger.debug(
        "RX filter readback: RCR=0x%08x (AAP=%d CBSSID_DATA=%d) "
        "RXFLTMAP0=0x%04x RXFLTMAP1=0x%04x RXFLTMAP2=0x%04x TCR=0x%08x CR=0x%08x",
        rcr, 1 if rcr & BIT_AAP else 0, 1 if rcr & BIT_CBSSID_DATA else 0,
        transport.read16(REG_RXFLTMAP0), transport.read16(REG_RXFLTMAP1),
        transport.read16(REG_RXFLTMAP2), transport.read32(0x0604),
        transport.read32(REG_CR),
    )
