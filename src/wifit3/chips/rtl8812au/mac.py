"""RTL8812AU MAC power-on flow (M1 scope = bring the chip up far enough
to accept a firmware upload).

Mirrors the cold-boot path of `rtw88xxa_power_on` (rtw88xxa.c:1006)
through to right before `rtw_download_firmware`. Post-FW MAC/PHY init
and channel tune are out of M1 scope.

Reference (rtw88-source-v6.18):
    mac.c:62       rtw_mac_pre_system_cfg
    mac.c:272      rtw_mac_power_switch
    mac.c:355      __rtw_mac_init_system_cfg_legacy
    rtw88xxa.c:391 rtw88xxa_llt_init
    rtw88xxa.c:1006 rtw88xxa_power_on (lines 1036..1067 in particular)

8812a-specific divergence vs 8821a:
    * Pre-pwr_seq RF reset writes (REG_RF_CTRL=5,7; REG_RF_B_CTRL=5,7).
      Lines 1037..1041. 2T2R chip, hence both paths.
    * Uses card_enable_flow_8812a tables (different from 8821a).
    * FIFO config differs (page_size=512 vs 256, rsvd_drv_pg_num=9 vs 8).
"""

from __future__ import annotations

import logging
import time
from enum import Enum

from .constants import (
    BIT_DIS_TSF_UDT,
    BIT_DMA_BURST_CNT,
    BIT_DMA_BURST_SIZE_512,
    BIT_DMA_MODE,
    BIT_DROP_DATA_EN,
    BIT_EN_BCN_FUNCTION,
    BIT_EN_SIC,
    BIT_EN_SINGLE_APMDU,
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
    PBP_64,
    PBP_512,
    PBP_RX_MASK,
    PBP_TX_MASK,
    PG_TBL_3BO_EXQ_NUM,
    PG_TBL_3BO_GAPQ_NUM,
    PG_TBL_3BO_HQ_NUM,
    PG_TBL_3BO_LQ_NUM,
    PG_TBL_3BO_NQ_NUM,
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
    REG_BCNTCFG,
    REG_CR,
    REG_CR_OFF_VALUE,
    REG_DRVERLYINT,
    REG_DWBCN0_CTRL,
    REG_EDCA_BE_PARAM,
    REG_EDCA_BK_PARAM,
    REG_EDCA_VI_PARAM,
    REG_EDCA_VO_PARAM,
    REG_FWHW_TXQ_CTRL,
    REG_GPIO_MUXCFG,
    REG_HIMR0,
    REG_HIMR1,
    REG_HMETFR,
    REG_HWSEQ_CTRL,
    REG_RCR,
    REG_LDO_SWR_CTRL,
    REG_LLT_INIT,
    REG_MAC_SPEC_SIFS,
    REG_MAR,
    REG_MAX_AGGR_NUM,
    REG_BCNQ_BDNY,
    REG_MGQ_BDNY,
    REG_PBP,
    REG_PIFS,
    REG_RETRY_LIMIT,
    REG_RF_B_CTRL,
    REG_RF_CTRL,
    REG_RQPN,
    REG_RQPN_NPQ,
    REG_RRSR,
    REG_RSV_CTRL,
    REG_RX_PKT_LIMIT,
    REG_RXDMA_AGG_PG_TH,
    REG_RXDMA_MODE,
    REG_RXDMA_STATUS,
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
    REPORT_BUF,
    RF_CTRL_RESET_STEP1,
    RF_CTRL_RESET_STEP2,
    RQPN_3BO_BE,
    RQPN_3BO_BK,
    RQPN_3BO_HI,
    RQPN_3BO_MG,
    RQPN_3BO_VI,
    RQPN_3BO_VO,
    RXDMA_AGG_SIZE,
    RXDMA_AGG_TIMEOUT,
    RXFF_SIZE,
    SPS_SEL,
    USB_TX_AGG_DESC_NUM,
    WLAN_TBTT_TIME,
    BIT_LD_RQPN,
    BIT_RXDMA_AGG_EN,
)
from wifit3.chips.rtw88_base.firmware_legacy import FW_READY_LEGACY
from wifit3.chips.rtw88_base.registers import REG_MCUFW_CTRL

from .fifo import FifoConf, set_trx_fifo_info
from .power_seq import (
    CARD_DISABLE_FLOW_8812A,
    CARD_ENABLE_FLOW_8812A,
    INTF_USB,
    run_pwr_seq,
)
from .transport import RTL8812AUTransport

# REG_SYS_STATUS1+1 BIT(0) is the USB-only secondary "is the chip off?" tell
# (mac.c:293). When set, the chip considers itself powered down even if
# REG_CR is non-0xEA.
REG_SYS_STATUS1 = 0x00F4
BIT_USB_PWR_OFF = 1 << 0  # at REG_SYS_STATUS1+1

logger = logging.getLogger(__name__)


# --- 8812a-specific RF reset (rtw88xxa.c:1036..1041) -----------------------
def rf_reset_8812a(transport: RTL8812AUTransport) -> None:
    """Bring both RF paths out of reset before the power sequence runs.

    Comment from kernel: 'Revise for U2/U3 switch we can not update RF-A/B
    reset. Reset after MAC power on to prevent RF R/W error.' Sequence is
    write(5) then write(7) per path — clears then asserts SDM_RSTB.
    """
    transport.write8(REG_RF_CTRL, RF_CTRL_RESET_STEP1)
    transport.write8(REG_RF_CTRL, RF_CTRL_RESET_STEP2)
    transport.write8(REG_RF_B_CTRL, RF_CTRL_RESET_STEP1)
    transport.write8(REG_RF_B_CTRL, RF_CTRL_RESET_STEP2)


# --- pre-pwr_seq system cfg (mac.c:62) -------------------------------------
def mac_pre_system_cfg(transport: RTL8812AUTransport) -> None:
    """USB-on-8051 path of `rtw_mac_pre_system_cfg` (mac.c:62).

    For 8812A the function:
        1. clears REG_RSV_CTRL
        2. picks LDO_SEL vs SPS_SEL based on BIT_LDO of REG_SYS_CFG1
    """
    transport.write8(REG_RSV_CTRL, 0)
    sys_cfg1 = transport.read32(REG_SYS_CFG1)
    if sys_cfg1 & BIT_LDO:
        logger.debug("BIT_LDO set in REG_SYS_CFG1=0x%08x -> LDO_SEL", sys_cfg1)
        transport.write8(REG_LDO_SWR_CTRL, LDO_SEL)
    else:
        logger.debug("BIT_LDO clear in REG_SYS_CFG1=0x%08x -> SPS_SEL", sys_cfg1)
        transport.write8(REG_LDO_SWR_CTRL, SPS_SEL)


def _is_chip_powered(transport: RTL8812AUTransport) -> bool:
    """Mirror the USB+8051 branch of mac.c:291."""
    if transport.read8(REG_CR) == REG_CR_OFF_VALUE:
        return False
    if transport.read8(REG_SYS_STATUS1 + 1) & BIT_USB_PWR_OFF:
        return False
    return True


def mac_power_switch(transport: RTL8812AUTransport, pwr_on: bool) -> bool:
    """USB+8051 path of `rtw_mac_power_switch` (mac.c:272).

    Returns:
        True  if pwr_seq ran successfully (state changed)
        False if the device was already in the requested state (`-EALREADY`)
    """
    cur_pwr = _is_chip_powered(transport)
    if pwr_on == cur_pwr:
        logger.debug("mac_power_switch: already %s", "on" if pwr_on else "off")
        return False

    pwr_seq = CARD_ENABLE_FLOW_8812A if pwr_on else CARD_DISABLE_FLOW_8812A
    for sub in pwr_seq:
        run_pwr_seq(transport, sub, intf_mask=INTF_USB)
    return True


def mac_init_system_cfg_legacy(transport: RTL8812AUTransport) -> None:
    """Port of `__rtw_mac_init_system_cfg_legacy` (mac.c:355)."""
    transport.write8(REG_CR, 0xFF)
    time.sleep(0.002)
    transport.write8(REG_HWSEQ_CTRL, 0x7F)
    time.sleep(0.002)
    transport.write8_set(REG_SYS_CLKR, BIT_WAKEPAD_EN)
    cur = transport.read16(REG_GPIO_MUXCFG)
    transport.write16(REG_GPIO_MUXCFG, cur & ((~BIT_EN_SIC) & 0xFFFF))
    transport.write16(REG_CR, 0x02FF)


# --- warm-state probe (M5 + M2-c) ------------------------------------------
class ChipState(Enum):
    """How much of the bring-up the chip has already completed."""
    COLD = "cold"              # nothing — full bring-up needed
    FW_WARM = "fw_warm"        # FW running, MAC not enabled — skip M1 only
    FULLY_WARM = "fully_warm"  # FW + MAC — skip M1 + M2


def probe_chip_state(transport: RTL8812AUTransport) -> ChipState:
    """Diagnose what state a re-claimed chip is in.

    - `FW_READY_LEGACY` (REG_MCUFW_CTRL & 0xC6 == 0xC6) → FW is running.
    - `BIT_MACTXEN | BIT_MACRXEN` (REG_CR & 0xC0 == 0xC0) → post-FW MAC
      init has also run.

    Anything that fails to read returns COLD (safest fallback).
    """
    try:
        mcufw = transport.read32(REG_MCUFW_CTRL)
        cr = transport.read8(REG_CR)
    except (IOError, OSError):
        return ChipState.COLD
    fw_running = (mcufw & FW_READY_LEGACY) == FW_READY_LEGACY
    mac_enabled = (cr & (BIT_MACTXEN | BIT_MACRXEN)) == (BIT_MACTXEN | BIT_MACRXEN)
    if fw_running and mac_enabled:
        return ChipState.FULLY_WARM
    if fw_running:
        return ChipState.FW_WARM
    return ChipState.COLD


def is_chip_warm(transport: RTL8812AUTransport) -> bool:
    """True if the chip is in *any* warm tier (FW_WARM or FULLY_WARM).

    Kept for the M5-era callers that only need "skip the cold path?".
    Prefer :func:`probe_chip_state` if you want to distinguish tiers.
    """
    return probe_chip_state(transport) is not ChipState.COLD


# --- top-level cold-boot helpers -------------------------------------------
def mac_power_on(transport: RTL8812AUTransport) -> None:
    """Cold-plug MAC power-on flow for 8812A.

    Order:
        1. rf_reset_8812a       (8812a-specific, both RF paths out of reset)
        2. mac_pre_system_cfg   (clear RSV_CTRL, pick LDO/SPS sel)
        3. mac_power_switch(True) — card_enable_flow_8812a (pwr_seq)
        4. mac_init_system_cfg_legacy

    Raises if the chip reports it is already powered. Callers that want
    to handle a warm device should detect that BEFORE invoking this
    (e.g. via `is_chip_warm` once M2+ ships). The kernel's
    power-cycle-on-EALREADY path is intentionally *not* mirrored here.
    """
    rf_reset_8812a(transport)
    mac_pre_system_cfg(transport)
    changed = mac_power_switch(transport, True)
    if not changed:
        raise IOError(
            "mac_power_on: chip already powered. The caller should detect "
            "this with a warm-state probe and skip the bring-up instead."
        )
    mac_init_system_cfg_legacy(transport)


# --- LLT init (rtw88xxa.c:391) ---------------------------------------------
_LLT_POLL_MAX = 21


def llt_write(transport: RTL8812AUTransport, address: int, data: int) -> None:
    """Write one LLT entry, polling for completion.

    Mirrors `rtw88xxa_llt_write` (rtw88xxa.c:370). 32-bit REG_LLT_INIT encodes:
        bits[31:30] = BIT_LLT_WRITE_ACCESS (set during a write request)
        bits[23:16] = address (LLT page index, 0..255)
        bits[7:0]   = data    (next-page link value)
    The hardware clears bits[31:30] once the write commits.
    """
    value = BIT_LLT_WRITE_ACCESS | ((address & 0xFF) << 8) | (data & 0xFF)
    transport.write32(REG_LLT_INIT, value)
    for _ in range(_LLT_POLL_MAX):
        if not (transport.read32(REG_LLT_INIT) & (3 << 30)):
            return
    raise IOError(f"LLT write to entry {address} failed to complete (poll timeout)")


def llt_init(transport: RTL8812AUTransport, boundary: int) -> None:
    """Initialise the LLT page-link ring.

    Mirrors `rtw88xxa_llt_init` (rtw88xxa.c:391). For boundary=247 (8812A):
        * entries 0..245       → i+1                  (chain forward)
        * entry 246            → 0xFF                 (end of TX-FIFO half)
        * entries 247..254     → i+1                  (chain forward in RSVD half)
        * entry 255            → 247 (=boundary)      (wrap RSVD half to its head)
    """
    last_entry = 255
    for i in range(boundary - 1):
        llt_write(transport, i, i + 1)
    llt_write(transport, boundary - 1, 0xFF)
    for i in range(boundary, last_entry):
        llt_write(transport, i, i + 1)
    llt_write(transport, last_entry, boundary)


# --- pre-FW init (rtw88xxa.c:1055..1067 minus FW download) -----------------
def pre_fw_init(transport: RTL8812AUTransport) -> FifoConf:
    """Mirrors rtw88xxa.c:1055..1067.

    Runs after `mac_power_on` and before FW upload. Returns the FifoConf
    that the post-FW queue setup (M2-b) consumes.
    """
    fifo = set_trx_fifo_info()
    llt_init(transport, fifo.rsvd_boundary)
    transport.write32_set(REG_TXDMA_OFFSET_CHK, BIT_DROP_DATA_EN)
    return fifo


# ===========================================================================
# M2-b: post-FW MAC init (rtw88xxa.c:1083..1175)
# ===========================================================================
# Most helpers below are family-shared with 8821au — TODO hoist to
# rtw88_base/mac_common.py once a third legacy chip lands. For now,
# duplicate-with-8812a-tweaks to keep the bring-up momentum + isolated diff.

def init_queue_reserved_page(transport: RTL8812AUTransport, fifo: FifoConf) -> None:
    """Mirrors `rtw88xxau_init_queue_reserved_page` (rtw88xxa.c:418).

    8812A with 3 bulk-OUT endpoints uses page_table[3] = {hq=16, nq=0,
    lq=16, exq=0, gapq=1}. pubq = acq_pg_num - sum = 247 - 33 = 214.
    """
    hq = PG_TBL_3BO_HQ_NUM
    nq = PG_TBL_3BO_NQ_NUM
    lq = PG_TBL_3BO_LQ_NUM
    exq = PG_TBL_3BO_EXQ_NUM
    gapq = PG_TBL_3BO_GAPQ_NUM
    pubq = fifo.acq_pg_num - hq - lq - nq - exq - gapq

    val_npq = (nq & 0xFF) | ((exq & 0xFF) << 16)
    transport.write32(REG_RQPN_NPQ, val_npq)

    val_rqpn = (
        BIT_LD_RQPN
        | (hq & 0xFF)
        | ((lq & 0xFF) << 8)
        | ((pubq & 0xFF) << 16)
    )
    transport.write32(REG_RQPN, val_rqpn)


def init_tx_buffer_boundary(transport: RTL8812AUTransport, fifo: FifoConf) -> None:
    """Mirrors `rtw88xxau_init_tx_buffer_boundary` (rtw88xxa.c:455)."""
    b = fifo.rsvd_boundary & 0xFF
    transport.write8(REG_BCNQ_BDNY, b)
    transport.write8(REG_MGQ_BDNY, b)
    transport.write8(REG_WMAC_LBK_BF_HD, b)
    transport.write8(REG_TRXFF_BNDY, b)
    transport.write8(REG_DWBCN0_CTRL + 1, b)


def init_queue_priority(transport: RTL8812AUTransport) -> None:
    """Mirrors `rtw88xxau_init_queue_priority` (rtw88xxa.c:466) for 3-bulkout.

    Reads REG_TXDMA_PQ_MAP (keeps low 3 bits), OR's in the lane mappings
    from rqpn_table_8812a[3] = {HIGH, NORMAL, LOW, LOW, HIGH, HIGH},
    writes 16-bit back. (Different from 8821a's 2-bulkout mappings.)
    """
    txdma_pq_map = transport.read16(REG_TXDMA_PQ_MAP) & 0x7
    mappings = (
        (RQPN_3BO_HI, BIT_SHIFT_TXDMA_HIQ_MAP),
        (RQPN_3BO_MG, BIT_SHIFT_TXDMA_MGQ_MAP),
        (RQPN_3BO_BK, BIT_SHIFT_TXDMA_BKQ_MAP),
        (RQPN_3BO_BE, BIT_SHIFT_TXDMA_BEQ_MAP),
        (RQPN_3BO_VI, BIT_SHIFT_TXDMA_VIQ_MAP),
        (RQPN_3BO_VO, BIT_SHIFT_TXDMA_VOQ_MAP),
    )
    for value, shift in mappings:
        txdma_pq_map |= (value & BIT_MASK_TXDMA_MAP) << shift
    transport.write16(REG_TXDMA_PQ_MAP, txdma_pq_map)


# Exact REG_RCR airmon-ng writes for monitor [WIRE captures_rtw88_8812au/
# capture-1 frames 6891-6901] — identical to rtl8821au/rtl8822bu: AAP|APM|AM|AB
# (promiscuous) with CBSSID_BCN/CBSSID_DATA cleared.
RCR_MONITOR = 0xF410400F


def apply_monitor_rx_filter(transport: RTL8812AUTransport) -> None:
    """Force the monitor RX filter on BOTH cold + warm attach.

    The mac_tbl load + drv_info_cfg leave REG_RCR with byte0 0x0E (AAP/bit0
    CLEAR) — not promiscuous — so client→AP (ToDS) traffic incl. M2/M4 EAPOL is
    dropped (PMKID still works via M1, AP→client). The kernel overwrites RCR →
    0xf410400f for monitor; we never did, and the warm path skips mac init
    entirely. Apply it here (from _finish_attach) so it runs on both paths.
    Net-type stays MGD_LINKED — not the gate. Same fix as rtl8821au/rtl8822bu.
    [WIRE 8812au frames 6891-6901; SRC rtw88 reg.h:502-534]
    """
    transport.write32(REG_RCR, RCR_MONITOR)
    rcr = transport.read32(REG_RCR)
    logger.debug(
        "RX filter readback: RCR=0x%08x (AAP=%d CBSSID_DATA=%d)",
        rcr, 1 if rcr & 0x1 else 0, 1 if rcr & (1 << 6) else 0,
    )


def configure_rx_aggregation(transport: RTL8812AUTransport, *, log: bool = True) -> None:
    """Arm the USB RX-DMA aggregation accumulator into the kernel's monitor state.

    Mirrors `rtw_usb_dynamic_rx_agg_v2(enable=false)` (usb.c:893) — the 8812a/
    8821a RX-agg path. Left at the FW power-on default the RX-DMA page accumulator
    is never armed; once enough RX accumulates it wedges and bulk-IN goes
    permanently silent while register writes still land — a clean RX cliff with no
    USB error. We are always monitor/unassociated, so we write the disable values
    (size=0, timeout=1) → REG_RXDMA_AGG_PG_TH=0x0100, then set BIT_RXDMA_AGG_EN in
    REG_TXDMA_PQ_MAP (BIT(2), distinct from the queue-map lanes at bits 4+). The
    kernel re-writes this every ~2 s from rtw_watch_dog_work; `_rx_agg_watchdog`
    mirrors that cadence (this is the per-tick body, called with ``log=False``).
    [WIRE captures_rtw88_8812au/capture-1 frame 7649: payload 00 01 = 0x0100]
    """
    val16 = (RXDMA_AGG_SIZE & 0xFF) | ((RXDMA_AGG_TIMEOUT & 0xFF) << 8)
    transport.write16(REG_RXDMA_AGG_PG_TH, val16)
    transport.write8_set(REG_TXDMA_PQ_MAP, BIT_RXDMA_AGG_EN)
    if log:
        logger.debug(
            "RX-DMA aggregation armed (monitor): REG_RXDMA_AGG_PG_TH=0x%04x, "
            "BIT_RXDMA_AGG_EN set in REG_TXDMA_PQ_MAP", val16,
        )


def init_wmac_setting(transport: RTL8812AUTransport) -> None:
    """Mirrors `rtw88xxa_init_wmac_setting` (rtw88xxa.c:512)."""
    transport.write16(REG_RXFLTMAP0, 0xFFFF)
    transport.write16(REG_RXFLTMAP1, 0x0400)
    transport.write16(REG_RXFLTMAP2, 0xFFFF)
    transport.write32(REG_MAR, 0xFFFFFFFF)
    transport.write32(REG_MAR + 4, 0xFFFFFFFF)


# RXFLTMAP1 gates ctrl subtypes; init_wmac_setting leaves it at 0x0400 (ps-poll only).
# Bit 13 = ACK, opened on demand so monitor RX can see the AP's ACK to our injects.
RXFLTMAP1_ACK = 1 << 13


def admit_ack_frames(transport: RTL8812AUTransport) -> None:
    """RXFLTMAP1 |= BIT(13) — let RX see the AP's ACKs to our injects. Off by default."""
    transport.write16(REG_RXFLTMAP1, transport.read16(REG_RXFLTMAP1) | RXFLTMAP1_ACK)


def drop_ack_frames(transport: RTL8812AUTransport) -> None:
    """Clear RXFLTMAP1 BIT(13) — restore the default monitor ctrl filter."""
    transport.write16(REG_RXFLTMAP1, transport.read16(REG_RXFLTMAP1) & ~RXFLTMAP1_ACK)


def init_adaptive_ctrl(transport: RTL8812AUTransport) -> None:
    """Mirrors `rtw88xxa_init_adaptive_ctrl` (rtw88xxa.c:522)."""
    transport.write32_mask(REG_RRSR, 0xFFFFF, 0xFFFF1)
    transport.write16(REG_RETRY_LIMIT, 0x3030)


def init_edca(transport: RTL8812AUTransport) -> None:
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


def init_beacon_parameters(transport: RTL8812AUTransport, *, btcoex: bool = False) -> None:
    """Mirrors `rtw88xxa_init_beacon_parameters` (rtw88xxa.c:557)."""
    val16 = (BIT_DIS_TSF_UDT << 8) | BIT_DIS_TSF_UDT
    if btcoex:
        val16 |= BIT_EN_BCN_FUNCTION
    transport.write16(REG_BCN_CTRL, val16)
    transport.write32_mask(REG_TBTT_PROHIBIT, 0xFFFFF, WLAN_TBTT_TIME)
    transport.write8(REG_DRVERLYINT, 0x05)
    transport.write8(REG_BCNDMATIM, 0x02)
    transport.write16(REG_BCNTCFG, 0x4413)


def tx_aggregation(transport: RTL8812AUTransport) -> None:
    """Mirrors `rtw88xxau_tx_aggregation` (rtw88xxa.c:545).

    For 8812A: only REG_DWBCN0_CTRL gets the desc_num poke. The 8821A-only
    REG_DWBCN1_CTRL second write is skipped (kernel guards it with
    `if chip->id == RTW_CHIP_TYPE_8821A`).
    """
    transport.write32_mask(REG_DWBCN0_CTRL, 0xF0, USB_TX_AGG_DESC_NUM)


def usb_interface_cfg(transport: RTL8812AUTransport) -> None:
    """Mirrors `rtw_usb_interface_cfg` → `rtw_usb_init_burst_pkt_len`
    (usb.c:846). AWUS036ACH negotiates as USB 2.0 HS → BIT_DMA_BURST_SIZE_512.
    Also re-asserts BIT_DROP_DATA_EN defensively (already set in pre_fw_init).
    """
    rxdma = BIT_DMA_BURST_CNT | BIT_DMA_MODE
    rxdma &= ~BIT_MASK_DMA_BURST_SIZE
    rxdma |= (BIT_DMA_BURST_SIZE_512 << BIT_SHIFT_DMA_BURST_SIZE) & BIT_MASK_DMA_BURST_SIZE
    transport.write8(REG_RXDMA_MODE, rxdma)
    cur = transport.read16(REG_TXDMA_OFFSET_CHK)
    transport.write16(REG_TXDMA_OFFSET_CHK, cur | (BIT_DROP_DATA_EN & 0xFFFF))


def post_fw_mac_init(transport: RTL8812AUTransport, fifo: FifoConf) -> None:
    """Run the MAC-only chunk of rtw88xxa_power_on after FW is running.

    Covers `rtw88xxa.c:1081..1175` (inclusive of MACTXEN|MACRXEN set),
    stopping BEFORE phy_bb_config / phy_rf_config / switch_band — those
    are M2-d.

    Pre-condition: `pre_fw_init` already ran (llt_init done) and FW is
    running (M1 validate passed).
    """
    # 1081
    transport.write8(REG_HMETFR, 0x0F)

    # 1083 — kernel does `rtw_load_table(chip->mac_tbl)` here. We defer
    # to M2-d so this milestone is just register pokes, no asset load.

    init_queue_reserved_page(transport, fifo)
    init_tx_buffer_boundary(transport, fifo)
    init_queue_priority(transport)

    # 1089
    transport.write16(REG_TRXFF_BNDY + 2, RXFF_SIZE - REPORT_BUF - 1)

    # 1092-1095 — 8812A-only: REG_PBP = PBP_512(TX) | PBP_64(RX)
    pbp = ((PBP_512 << 4) & PBP_TX_MASK) | ((PBP_64 << 0) & PBP_RX_MASK)
    transport.write8(REG_PBP, pbp)

    # 1097: REG_RX_DRVINFO_SZ — NOTE: this write was moved to the END of
    # post_mac_init_phy because mac_tbl (loaded in post_mac_init_phy)
    # clobbers part of REG_RCR (bytes 0..1 via 0x608=0x0E, 0x609=0x2A),
    # and PHY_STATUS reporting requires both REG_RX_DRVINFO_SZ AND
    # BIT_APP_PHYSTS in REG_RCR to be set together. Doing the pair at the
    # end matches the kernel's drv_info_cfg call ordering relative to
    # mac_tbl load.

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

    # 1119 — usb3 rx interval (kernel sets unconditionally; harmless on HS)
    transport.write8(REG_USB3_RXITV, 0x01)

    # 1122-1123
    transport.write16(REG_RXDMA_STATUS, 0x7400)
    transport.write8(REG_RXDMA_STATUS + 1, 0xF5)

    # 1126-1129 — 8812A path: 0x70 (vs 8821a's 0x5e)
    transport.write8(REG_AMPDU_MAX_TIME, 0x70)
    transport.write32(REG_AMPDU_MAX_LENGTH, 0xFFFFFFFF)
    transport.write8(REG_USTIME_TSF, 0x50)
    transport.write8(REG_USTIME_EDCA, 0x50)

    # 1135 — only for USB 3.0 SuperSpeed; AWUS036ACH is HS → skip.

    # 1139
    transport.write8_set(REG_SINGLE_AMPDU_CTRL, BIT_EN_SINGLE_APMDU)
    # 1142
    transport.write8(REG_RX_PKT_LIMIT, 0x18)
    # 1144
    transport.write8(REG_PIFS, 0x00)

    # 1146-1153 — 8812A (else) branch: max_aggr_num = 0x1F1F + clear FWHW BIT(7)
    transport.write16(REG_MAX_AGGR_NUM, 0x1F1F)
    transport.write8_clr(REG_FWHW_TXQ_CTRL, 1 << 7)
    # (8821A-specific FAST_EDCA write is skipped for 8812a.)

    # 1157
    transport.write8_set(REG_RSV_CTRL, (1 << 5) | (1 << 6))

    # 1160-1173 — ARFB tables 9/10/11/12 (same for both 8821a and 8812a)
    transport.write32(REG_ARFR0, 0x00000010)
    transport.write32(REG_ARFRH0, 0xFFFFF000)
    transport.write32(REG_ARFR1_V1, 0x00000010)
    transport.write32(REG_ARFRH1_V1, 0x003FF000)
    transport.write32(REG_ARFR2_V1, 0x00000015)
    transport.write32(REG_ARFRH2_V1, 0x003FF000)
    transport.write32(REG_ARFR3_V1, 0x00000015)
    transport.write32(REG_ARFRH3_V1, 0xFFCFF000)

    # 1175 — MACTXEN | MACRXEN (the M2-b pass-line)
    transport.write8_set(REG_CR, BIT_MACTXEN | BIT_MACRXEN)
