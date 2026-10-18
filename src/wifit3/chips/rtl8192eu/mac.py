"""RTL8192EU MAC init — endpoints, queue pages, MAC table, LLT, RX filter.

Cleanroom port of the 8192eu fileops MAC bring-up from
`driver_sources/rtl8xxxu-source-v6.19/`:

* `rtl8xxxu_config_endpoints_sie` / `_no_sie` — `core.c:1695-1743`
* `rtl8xxxu_init_queue_reserved_page` — `core.c:3815-3845`
* `rtl8xxxu_init_queue_priority`      — `core.c:2583-2714`
* `rtl8xxxu_init_mac`                 — `core.c:2186-2226`
* `rtl8xxxu_set_mac`                  — `core.c:3558-3582` (port 0, `REG_MACID`)
* `rtl8xxxu_auto_llt_table`           — `core.c:2558-2581`
* TX buffer boundary block            — `core.c:4021-4032`
* REG_PBP                             — `core.c:4034-4041` (never written on RTL8192E)
* RX-enable block                     — `core.c:4096-4154`
* beacon parameters                   — `core.c:4199-4211`
* adaptive controls                   — `core.c:4156-4197`
* CAM invalidate                      — `core.c:4234-4237`
* HW sequencer / BAR / fast EDCA      — `core.c:4252-4259`
* RTL8192E chip-init tail             — `core.c:4264-4275`
* NAV upper / USB HRPWM / mgmt-ACK    — `core.c:4310-4332`

`rtl8xxxu_init_device` (`core.c:3918-4412`) runs the queue page partition,
DMA-lane routing and the RX page boundary *before* the FW upload
(`core.c:3952-3962`), so `pre_fw_queue_init` is called pre-FW;
`post_fw_mac_init` is the MAC table alone (`core.c:3982`) and
`post_phy_mac_init` is the TX-boundary/LLT/quirks block that follows PHY/RF
init (`core.c:4021-4054`). `CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE` are set by
`rtl8192eu_power_on` (`8192e.c:1583-1589`) — there is no separate enable here.

The 8192eu `.llt_init` is `rtl8xxxu_auto_llt_table` (`8192e.c:1729`), not
the 8188e `rtl8xxxu_init_llt_table`: the hardware builds the LLT ring
itself, so we set `AUTO_LLT_INIT_LLT` in `REG_AUTO_LLT` and poll for the
bit to self-clear rather than writing 176 entries by hand.
"""
from __future__ import annotations

import logging
from typing import NamedTuple

from .constants import (
    AUTO_LLT_INIT_LLT,
    BEACON_DISABLE_TSF_UPDATE,
    BEACON_DMA_ATIME_INT_TIME,
    CAM_CMD_POLLING,
    CR_MAC_RX_ENABLE,
    CR_MAC_TX_ENABLE,
    DRIVER_EARLY_INT_TIME,
    FWHW_TXQ_CTRL_AMPDU_RETRY,
    FWHW_TXQ_CTRL_XMIT_MGMT_ACK,
    MCU_WINT_INIT_READY,
    NAV_UPPER_UNIT,
    NORMAL_SIE_EP_TX_HIGH_MASK,
    NORMAL_SIE_EP_TX_LOW_MASK,
    NORMAL_SIE_EP_TX_NORMAL_MASK,
    RCR_MONITOR,
    REG_ACKTO,
    REG_AUTO_LLT,
    REG_BAR_MODE_CTRL,
    REG_BEACON_CTRL,
    REG_BEACON_DMA_TIME,
    REG_BEACON_TCFG,
    REG_CAM_CMD,
    REG_CR,
    REG_DARFRC,
    REG_DRIVER_EARLY_INT,
    REG_EDCA_BE_PARAM,
    REG_EDCA_BK_PARAM,
    REG_EDCA_VI_PARAM,
    REG_EDCA_VO_PARAM,
    REG_FAST_EDCA_CTRL,
    REG_FWHW_TXQ_CTRL,
    REG_HIMR0,
    REG_HIMR1,
    REG_HWSEQ_CTRL,
    REG_MACID,
    REG_MAC_SPEC_SIFS,
    REG_MCU_FW_DL,
    REG_NAV_UPPER,
    REG_NORMAL_SIE_EP_TX,
    REG_RARFRC,
    REG_RCR,
    REG_RESPONSE_RATE_SET,
    REG_RETRY_LIMIT,
    REG_RQPN,
    REG_RQPN_NPQ,
    REG_RX_DRVINFO_SZ,
    REG_RXFLTMAP1,
    REG_SIFS_CCK,
    REG_SIFS_OFDM,
    REG_SPEC_SIFS,
    REG_TBTT_PROHIBIT,
    REG_TDECTRL,
    REG_TRXFF_BNDY,
    REG_TXPKTBUF_BCNQ_BDNY,
    REG_TXPKTBUF_MGQ_BDNY,
    REG_TXPKTBUF_WMAC_LBK_BF_HD,
    REG_TRXDMA_CTRL,
    REG_USB_HRPWM,
    RESPONSE_RATE_BITMAP_ALL,
    RESPONSE_RATE_RRSR_CCK_ONLY_1M,
    RETRY_LIMIT_LONG_MASK,
    RETRY_LIMIT_LONG_SHIFT,
    RETRY_LIMIT_SHORT_MASK,
    RETRY_LIMIT_SHORT_SHIFT,
    RQPN_EPQ_SHIFT,
    RQPN_HI_PQ_SHIFT,
    RQPN_LOAD,
    RQPN_LO_PQ_SHIFT,
    RQPN_NPQ_SHIFT,
    RQPN_PUB_PQ_SHIFT,
    SPEC_SIFS_CCK_MASK,
    SPEC_SIFS_CCK_SHIFT,
    SPEC_SIFS_OFDM_MASK,
    SPEC_SIFS_OFDM_SHIFT,
    TRXDMA_CTRL_BEQ_SHIFT,
    TRXDMA_CTRL_BKQ_SHIFT,
    TRXDMA_CTRL_HIQ_SHIFT,
    TRXDMA_CTRL_MGQ_SHIFT,
    TRXDMA_CTRL_VIQ_SHIFT,
    TRXDMA_CTRL_VOQ_SHIFT,
    TRXDMA_QUEUE_HIGH,
    TRXDMA_QUEUE_LOW,
    TRXDMA_QUEUE_NORMAL,
    TX_PAGE_NUM_HI_PQ_8192E,
    TX_PAGE_NUM_LO_PQ_8192E,
    TX_PAGE_NUM_NORM_PQ_8192E,
    TX_TOTAL_PAGE_NUM_8192E,
    TXDESC_QUEUE_BE,
    TXDESC_QUEUE_BEACON,
    TXDESC_QUEUE_BK,
    TXDESC_QUEUE_MGNT,
    TXDESC_QUEUE_VI,
    TXDESC_QUEUE_VO,
)
from .phy import usb_quirks
from .phy_tables import MAC_INIT_TABLE_8192E
from .transport import RTL8192EUTransport

logger = logging.getLogger(__name__)

# `.trxff_boundary = 0x3cff` — `8192e.c:1761`. Not exported by constants.py.
TRXFF_BOUNDARY_8192E = 0x3CFF

# `AUTO_LLT_INIT_LLT` poll cap — `for (i = 500; i; i--)` (`core.c:2568`).
AUTO_LLT_POLL_MAX = 500

# Register addresses the RTL8192E init tail touches; not exported by constants.py.
REG_MAR = 0x0620         # regs.h:791
REG_QUEUE_CTRL = 0x04C6  # regs.h:628
REG_ACLK_MON = 0x003E    # regs.h:140


class TxQueues(NamedTuple):
    """The TX queues `rtl8xxxu_config_endpoints_*` reports (`priv->ep_tx_*_queue`)."""

    high: bool
    normal: bool
    low: bool

    @property
    def count(self) -> int:
        return int(self.high) + int(self.normal) + int(self.low)


class TxQueueLanes(NamedTuple):
    """The DMA lane `rtl8xxxu_init_queue_priority` gives each TX queue.

    `hiq`/`mgq` are always equal on this part, so one field covers the HIGH lane
    (`core.c:2583-2682`).
    """

    vo: int
    vi: int
    be: int
    bk: int
    mg: int
    hi: int


class TxLaneEndpoints(NamedTuple):
    """Bulk-OUT endpoint *index* per TX queue — `priv->pipe_out[...]` (`core.c:2695-2710`).

    MGNT and BEACON always share `out_ep[0]`; an AC queue uses the endpoint its lane
    was routed to. The index is that lane's rank among the *enabled* lanes ordered
    HIGH > NORMAL > LOW, which is the value both the kernel's hard-coded `0`/`1`
    (`core.c:2645-2649`) and its `lane ^ 3` (`core.c:2609-2611`) work out to.
    """

    mgnt: int
    beacon: int
    be: int
    bk: int
    vi: int
    vo: int


def apply_mac_init_table(t: RTL8192EUTransport) -> int:
    """Port of `rtl8xxxu_init_mac` (core.c:2186-2226).

    Iterates `fops->mactable` (8192e.c:19-47). The chip switch below the
    loop (core.c:2210-2223) hits `default:` for RTL8192E — unlike RTL8188E
    it writes no `REG_MAX_AGGR_NUM`. Returns the number of byte writes.
    """
    count = 0
    for reg, val in MAC_INIT_TABLE_8192E:
        t.write8(reg, val)
        count += 1
    return count


def config_endpoints(t: RTL8192EUTransport, nr_out_eps: int) -> TxQueues:
    """Port of `rtl8xxxu_config_endpoints_sie` + `_no_sie` (core.c:1695-1743).

    Reads the TX-queue enable bits from `REG_NORMAL_SIE_EP_TX` (the 8192eu
    `identify_chip` path, `8192e.c:500`). A part that leaves the register at 0
    (this TL-WN823N) falls back to the USB bulk-OUT endpoint count
    (`8192e.c:505-506`); zero endpoints is the kernel's `-ENOTSUPP`.
    """
    sie = t.read16(REG_NORMAL_SIE_EP_TX)
    queues = TxQueues(
        high=bool(sie & NORMAL_SIE_EP_TX_HIGH_MASK),
        normal=bool(sie & NORMAL_SIE_EP_TX_NORMAL_MASK),
        low=bool(sie & NORMAL_SIE_EP_TX_LOW_MASK),
    )
    if queues.count:
        return queues
    if nr_out_eps >= 3:
        return TxQueues(high=True, normal=True, low=True)
    if nr_out_eps == 2:
        return TxQueues(high=True, normal=True, low=False)
    if nr_out_eps == 1:
        return TxQueues(high=True, normal=False, low=False)
    raise ValueError(f"RTL8192EU: unsupported USB TX end-point count {nr_out_eps}")


def init_queue_reserved_page(t: RTL8192EUTransport, queues: TxQueues) -> None:
    """Port of `rtl8xxxu_init_queue_reserved_page` (core.c:3815-3845).

    8192eu page counts: hi=0x08, lo=0x0c, norm=0x00, total=0xf3
    (rtl8xxxu.h:61-64). The public pool takes everything left over.
    """
    hq = TX_PAGE_NUM_HI_PQ_8192E if queues.high else 0
    lq = TX_PAGE_NUM_LO_PQ_8192E if queues.low else 0
    nq = TX_PAGE_NUM_NORM_PQ_8192E if queues.normal else 0
    eq = 0

    val32 = (nq << RQPN_NPQ_SHIFT) | (eq << RQPN_EPQ_SHIFT)
    t.write32(REG_RQPN_NPQ, val32)

    pubq = TX_TOTAL_PAGE_NUM_8192E - hq - lq - nq - 1
    val32 = (
        RQPN_LOAD
        | (hq << RQPN_HI_PQ_SHIFT)
        | (lq << RQPN_LO_PQ_SHIFT)
        | (pubq << RQPN_PUB_PQ_SHIFT)
    )
    t.write32(REG_RQPN, val32)


def queue_lanes(queues: TxQueues) -> TxQueueLanes:
    """The queue → DMA-lane switch of `rtl8xxxu_init_queue_priority` (`core.c:2596-2682`)."""
    if queues.count == 1:
        if queues.high:
            hi = TRXDMA_QUEUE_HIGH
        elif queues.low:
            hi = TRXDMA_QUEUE_LOW
        elif queues.normal:
            hi = TRXDMA_QUEUE_NORMAL
        else:
            raise ValueError("RTL8192EU: TX queue flags disagree with the count")
        return TxQueueLanes(vo=hi, vi=hi, be=hi, bk=hi, mg=hi, hi=hi)
    if queues.count == 2:
        if queues.high and queues.low:
            hi, lo = TRXDMA_QUEUE_HIGH, TRXDMA_QUEUE_LOW
        elif queues.normal and queues.low:
            hi, lo = TRXDMA_QUEUE_NORMAL, TRXDMA_QUEUE_LOW
        elif queues.high and queues.normal:
            hi, lo = TRXDMA_QUEUE_HIGH, TRXDMA_QUEUE_NORMAL
        else:
            raise ValueError("RTL8192EU: TX queue flags disagree with the count")
        return TxQueueLanes(vo=hi, vi=hi, be=lo, bk=lo, mg=hi, hi=hi)
    if queues.count == 3:
        return TxQueueLanes(
            vo=TRXDMA_QUEUE_HIGH,
            vi=TRXDMA_QUEUE_NORMAL,
            be=TRXDMA_QUEUE_LOW,
            bk=TRXDMA_QUEUE_LOW,
            mg=TRXDMA_QUEUE_HIGH,
            hi=TRXDMA_QUEUE_HIGH,
        )
    raise ValueError(f"RTL8192EU: unsupported TX queue count {queues.count}")


def tx_lane_endpoints(queues: TxQueues) -> TxLaneEndpoints:
    """The bulk-OUT endpoint index each TX queue writes to (`core.c:2688-2710`)."""
    lanes = queue_lanes(queues)
    enabled = [
        lane
        for lane, is_enabled in (
            (TRXDMA_QUEUE_HIGH, queues.high),
            (TRXDMA_QUEUE_NORMAL, queues.normal),
            (TRXDMA_QUEUE_LOW, queues.low),
        )
        if is_enabled
    ]
    rank = {lane: index for index, lane in enumerate(enabled)}
    return TxLaneEndpoints(
        mgnt=0,
        beacon=0,
        be=rank[lanes.be],
        bk=rank[lanes.bk],
        vi=rank[lanes.vi],
        vo=rank[lanes.vo],
    )


def endpoint_index_for_queue(queue: int, lanes: TxLaneEndpoints) -> int:
    """Index into the enumerated bulk-OUT endpoints for `queue` (`core.c:2695-2710`)."""
    index = {
        TXDESC_QUEUE_BEACON: lanes.beacon,
        TXDESC_QUEUE_MGNT: lanes.mgnt,
        TXDESC_QUEUE_BE: lanes.be,
        TXDESC_QUEUE_BK: lanes.bk,
        TXDESC_QUEUE_VI: lanes.vi,
        TXDESC_QUEUE_VO: lanes.vo,
    }
    if queue not in index:
        raise ValueError(f"RTL8192EU: no bulk-OUT lane for TX queue 0x{queue:02x}")
    return index[queue]


def init_queue_priority(t: RTL8192EUTransport, queues: TxQueues) -> None:
    """Port of `rtl8xxxu_init_queue_priority` (`core.c:2583-2714`).

    Maps the four AC queues onto the `REG_TRXDMA_CTRL` DMA lanes by `ep_tx_count`.
    The kernel's `priv->pipe_out[...]` table is not a register; the same routing is
    reproduced by `tx_lane_endpoints`.
    """
    lanes = queue_lanes(queues)

    val16 = t.read16(REG_TRXDMA_CTRL) & 0x7
    val16 |= (
        (lanes.vo << TRXDMA_CTRL_VOQ_SHIFT)
        | (lanes.vi << TRXDMA_CTRL_VIQ_SHIFT)
        | (lanes.be << TRXDMA_CTRL_BEQ_SHIFT)
        | (lanes.bk << TRXDMA_CTRL_BKQ_SHIFT)
        | (lanes.mg << TRXDMA_CTRL_MGQ_SHIFT)
        | (lanes.hi << TRXDMA_CTRL_HIQ_SHIFT)
    )
    t.write16(REG_TRXDMA_CTRL, val16 & 0xFFFF)


def pre_fw_queue_init(t: RTL8192EUTransport, queues: TxQueues) -> TxLaneEndpoints:
    """Queue page partition + DMA lanes + RX page boundary, before the FW upload.

    Kernel order: `init_queue_reserved_page` (`core.c:3952`),
    `init_queue_priority` (`core.c:3954`), then `REG_TRXFF_BNDY + 2 =
    trxff_boundary` (`core.c:3962`) — all ahead of `download_firmware`
    (`core.c:3965`). Returns the queue → bulk-OUT endpoint table for the TX path.
    """
    init_queue_reserved_page(t, queues)
    init_queue_priority(t, queues)
    set_trxff_rx_page_boundary(t)
    return tx_lane_endpoints(queues)


def set_trxff_rx_page_boundary(t: RTL8192EUTransport) -> None:
    """Write the RX page boundary `REG_TRXFF_BNDY + 2 = 0x3cff`.

    `8192e.c:1761` (`.trxff_boundary`, vs 8188e's 0x25ff). The kernel does it
    pre-FW at `core.c:3962`.
    """
    t.write16(REG_TRXFF_BNDY + 2, TRXFF_BOUNDARY_8192E)


def set_tx_buffer_boundary(t: RTL8192EUTransport) -> None:
    """Port of the TX buffer boundary block (core.c:4021-4032).

    `val8 = fops->total_page_num + 1` (0xf4 for 8192eu), written to the five
    TXPKTBUF/TRXFF boundary registers.
    """
    val8 = (TX_TOTAL_PAGE_NUM_8192E + 1) & 0xFF
    t.write8(REG_TXPKTBUF_BCNQ_BDNY, val8)
    t.write8(REG_TXPKTBUF_MGQ_BDNY, val8)
    t.write8(REG_TXPKTBUF_WMAC_LBK_BF_HD, val8)
    t.write8(REG_TRXFF_BNDY, val8)
    t.write8(REG_TDECTRL + 1, val8)


def set_pbp(t: RTL8192EUTransport) -> None:
    """No-op for RTL8192E — `core.c:4034-4041` skips `REG_PBP`.

    The vendor drivers set PBP for every part *except* 8192e ("no
    explanation for this in any of the sources"), and the 8192eu fops
    carries no `.pbp_rx`/`.pbp_tx` (8192e.c:1721-1767), so the computed
    value would be 0 anyway.
    """


def auto_llt_table(t: RTL8192EUTransport) -> None:
    """Port of `rtl8xxxu_auto_llt_table` (core.c:2558-2581) — 8192eu `.llt_init`.

    Sets `AUTO_LLT_INIT_LLT` in `REG_AUTO_LLT`; the hardware builds the LLT
    ring and self-clears the bit. Polls up to 500 times (core.c:2568).
    """
    val32 = t.read32(REG_AUTO_LLT) | AUTO_LLT_INIT_LLT
    t.write32(REG_AUTO_LLT, val32)
    for _ in range(AUTO_LLT_POLL_MAX):
        if (t.read32(REG_AUTO_LLT) & AUTO_LLT_INIT_LLT) == 0:
            return
    raise IOError("LLT auto-init did not clear AUTO_LLT_INIT_LLT")


def post_fw_mac_init(t: RTL8192EUTransport) -> None:
    """Apply the RTL8192E MAC init table (`rtl8xxxu_init_mac`, core.c:2186-2226).

    This is the whole `macpower == false` MAC-table block (`core.c:3982`); the
    TX-boundary/LLT/quirks tail it used to carry runs later, in
    `post_phy_mac_init`.
    """
    logger.debug("applying 8192eu MAC init table (%d byte writes)", len(MAC_INIT_TABLE_8192E))
    apply_mac_init_table(t)


def post_phy_mac_init(t: RTL8192EUTransport) -> None:
    """TX buffer boundary + PBP + auto-LLT + USB quirks (core.c:4021-4054).

    Runs after `rtl8xxxu_init_phy_bb`/`init_phy_rf` and the RFSW control
    (`core.c:3988-4014`), inside the always-taken `!macpower` block for this
    `.needs_full_init` part.
    """
    set_tx_buffer_boundary(t)
    set_pbp(t)
    auto_llt_table(t)
    usb_quirks(t)


def set_mac_addr(t: RTL8192EUTransport, mac: bytes) -> None:
    """Port of `rtl8xxxu_set_mac` (core.c:3558-3582), port 0.

    Writes the six address bytes of `priv->vifs[0]->addr` to
    `REG_MACID .. REG_MACID+5`, one byte at a time. The MAC HW-ACKs frames
    addressed to this address even in monitor mode, which is what
    `enter_active_monitor` uses to impersonate a client STA.
    """
    if len(mac) != 6:
        raise ValueError(f"RTL8192EU: malformed MAC address {mac!r}")
    for i in range(6):
        t.write8(REG_MACID + i, mac[i])


def is_chip_warm(t: RTL8192EUTransport) -> bool:
    """Return True if the chip looks like a previous wifit3 session
    left it FW-running + MAC-enabled.

    Two signals:
      1. `MCU_WINT_INIT_READY` (bit 6 of REG_MCU_FW_DL) — set by
         `start_firmware` once the 8051 boots the uploaded FW image.
      2. `CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE` (bits 6+7 of REG_CR) —
         set by `rtl8192eu_power_on` (`8192e.c:1583-1589`).

    Both bits together mean we're past the FW upload and power-on, so we can
    skip the FW upload and MAC/PHY init and just resume RX/TX. Returns False on
    any USB read error — safer to do a full cold boot than to mistakenly
    skip init steps the chip actually needs.
    """
    try:
        mcu_fw = t.read32(REG_MCU_FW_DL)
        cr = t.read16(REG_CR)
    except (IOError, OSError):
        return False
    fw_running = bool(mcu_fw & MCU_WINT_INIT_READY)
    mac_enabled = (cr & (CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE)) == (
        CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE
    )
    return fw_running and mac_enabled


def apply_monitor_rx_filter(t: RTL8192EUTransport) -> None:
    """Reassert the monitor RCR. Called on BOTH cold + warm attach.

    `enable_rx_data_path` writes RCR_MONITOR (incl. RCR_ACCEPT_AP/promiscuous),
    but it only runs on the cold path. The warm path skips it, so a chip left
    by a prior session keeps a non-promiscuous RCR and drops client→AP (ToDS)
    frames — incl. M2/M4 EAPOL. RCR_MONITOR already includes
    RCR_APPEND_PHYSTAT, so RSSI is unaffected. Mirrors the rtl8821au/rtl8822bu fix.
    """
    t.write32(REG_RCR, RCR_MONITOR)
    rcr = t.read32(REG_RCR)
    logger.debug("RX filter readback: RCR=0x%08x (ACCEPT_AP=%d)",
                rcr, 1 if rcr & 0x1 else 0)


# ACK is ctrl subtype 13; RXFLTMAP1 bit N gates ctrl subtype N. The 8192eu fops
# sets no `.init_reg_rxfltmap` (8192e.c:1721-1767), so REG_RXFLTMAP1 stays at its HW
# default and ACKs are filtered. Admit bit13 on demand so monitor RX sees the AP's
# ACK to our injects. RCR already has ACCEPT_CTRL_FRAME.
RXFLTMAP1_ACK = 1 << 13


def admit_ack_frames(t: RTL8192EUTransport) -> None:
    """RXFLTMAP1 |= BIT(13) — let RX see the AP's ACKs to our injects. Off by default."""
    t.write16(REG_RXFLTMAP1, t.read16(REG_RXFLTMAP1) | RXFLTMAP1_ACK)


def drop_ack_frames(t: RTL8192EUTransport) -> None:
    """Clear RXFLTMAP1 BIT(13) — restore the default monitor ctrl filter."""
    t.write16(REG_RXFLTMAP1, t.read16(REG_RXFLTMAP1) & ~RXFLTMAP1_ACK)


def enable_rx_data_path(t: RTL8192EUTransport) -> None:
    """Port of the RX-enable block (core.c:4096-4154).

    After this returns the chip accepts management/data frames and the
    bulk-IN endpoint starts delivering RX URBs.
    """
    # PHY status appended to each RX: 4 × 8 = 32 bytes (core.c:4096-4100).
    t.write8(REG_RX_DRVINFO_SZ, 4)

    # RTL8192E interrupt config: mask both interrupt sets (core.c:4102-4104).
    t.write32(REG_HIMR0, 0x00)
    t.write32(REG_HIMR1, 0x00)

    # The firehose: REG_RCR (core.c:4130-4133). RCR_MONITOR is this port's
    # monitor superset (the kernel value here is station-mode).
    t.write32(REG_RCR, RCR_MONITOR)

    # No `.init_reg_rxfltmap` on 8192eu (8192e.c:1721-1767) → else branch:
    # accept all multicast via the MAC address filter (core.c:4148-4154).
    t.write32(REG_MAR, 0xFFFFFFFF)
    t.write32(REG_MAR + 4, 0xFFFFFFFF)


def init_beacon_params(t: RTL8192EUTransport) -> None:
    """Port of the beacon-parameter block (core.c:4199-4211).

    Disables TSF update in the beacon control, sets the TBTT prohibit window,
    the driver early-interrupt time, the beacon DMA time and the beacon TCFG.
    The `REG_DRIVER_EARLY_INT` write is skipped only for 8188F/8710B/8192F
    (core.c:4205-4206), so it applies to RTL8192E.
    """
    val16 = BEACON_DISABLE_TSF_UPDATE | (BEACON_DISABLE_TSF_UPDATE << 8)
    t.write16(REG_BEACON_CTRL, val16)
    t.write16(REG_TBTT_PROHIBIT, 0x6404)
    t.write8(REG_DRIVER_EARLY_INT, DRIVER_EARLY_INT_TIME)
    t.write8(REG_BEACON_DMA_TIME, BEACON_DMA_ATIME_INT_TIME)
    t.write16(REG_BEACON_TCFG, 0x660F)


def init_rtl8192e_tail(t: RTL8192EUTransport) -> None:
    """Port of the RTL8192E-only init tail (core.c:4264-4275).

    `REG_QUEUE_CTRL[3] = 0`: RTS BW follows the data BW / CCA result.
    `REG_ACLK_MON = 0`: reset the USB mode-switch setting.
    """
    t.write8(REG_QUEUE_CTRL, t.read8(REG_QUEUE_CTRL) & ~(1 << 3) & 0xFF)
    t.write8(REG_ACLK_MON, 0x00)


def _set_retry(t: RTL8192EUTransport, short_retry: int, long_retry: int) -> None:
    """Port of `rtl8xxxu_set_retry` (core.c:1627-1638)."""
    val16 = ((short_retry << RETRY_LIMIT_SHORT_SHIFT) & RETRY_LIMIT_SHORT_MASK) | (
        (long_retry << RETRY_LIMIT_LONG_SHIFT) & RETRY_LIMIT_LONG_MASK
    )
    t.write16(REG_RETRY_LIMIT, val16 & 0xFFFF)


def _set_spec_sifs(t: RTL8192EUTransport, cck: int, ofdm: int) -> None:
    """Port of `rtl8xxxu_set_spec_sifs` (core.c:1640-1649)."""
    val16 = ((cck << SPEC_SIFS_CCK_SHIFT) & SPEC_SIFS_CCK_MASK) | (
        (ofdm << SPEC_SIFS_OFDM_SHIFT) & SPEC_SIFS_OFDM_MASK
    )
    t.write16(REG_SPEC_SIFS, val16 & 0xFFFF)


def init_adaptive_controls(t: RTL8192EUTransport) -> None:
    """Port of the "Init adaptive controls" block (core.c:4159-4197).

    RRSR (CCK-only 1 M response rate), the special-SIFS/retry trio, the three
    SIFS registers, the four EDCA TXOP parameters, the DARFRC/RARFRC rate-
    fallback tables, the AMPDU retry bit and the ACK timeout.
    """
    # Set the response rate — RRSR CCK-only 1 M (core.c:4159-4162).
    val32 = t.read32(REG_RESPONSE_RATE_SET)
    val32 &= ~RESPONSE_RATE_BITMAP_ALL & 0xFFFFFFFF
    val32 |= RESPONSE_RATE_RRSR_CCK_ONLY_1M
    t.write32(REG_RESPONSE_RATE_SET, val32 & 0xFFFFFFFF)

    # CCK = 0x0a, OFDM = 0x10 (core.c:4165-4167).
    _set_spec_sifs(t, 0x10, 0x10)
    _set_retry(t, 0x30, 0x30)
    _set_spec_sifs(t, 0x0a, 0x10)

    # Init EDCA (core.c:4172-4184): MAC spec SIFS, CCK SIFS, OFDM SIFS, then
    # the four EDCA TXOP parameters.
    t.write16(REG_MAC_SPEC_SIFS, 0x100a)
    t.write16(REG_SIFS_CCK, 0x100a)
    t.write16(REG_SIFS_OFDM, 0x100a)
    t.write32(REG_EDCA_BE_PARAM, 0x005EA42B)
    t.write32(REG_EDCA_BK_PARAM, 0x0000A44F)
    t.write32(REG_EDCA_VI_PARAM, 0x005EA324)
    t.write32(REG_EDCA_VO_PARAM, 0x002FA226)

    # Data auto rate fallback retry count (core.c:4187-4190).
    t.write32(REG_DARFRC, 0x00000000)
    t.write32(REG_DARFRC + 4, 0x10080404)
    t.write32(REG_RARFRC, 0x04030201)
    t.write32(REG_RARFRC + 4, 0x08070605)

    # AMPDU retry (core.c:4192-4194, byte-wide RMW).
    val8 = t.read8(REG_FWHW_TXQ_CTRL) | FWHW_TXQ_CTRL_AMPDU_RETRY
    t.write8(REG_FWHW_TXQ_CTRL, val8 & 0xFF)

    # ACK timeout (core.c:4197).
    t.write8(REG_ACKTO, 0x40)


def invalidate_cam_entries(t: RTL8192EUTransport) -> None:
    """Invalidate all CAM entries (core.c:4234-4237).

    `CAM_CMD_POLLING | BIT(30)`; BIT(30) is undocumented in the kernel source.
    """
    t.write32(REG_CAM_CMD, CAM_CMD_POLLING | (1 << 30))


def init_hw_sequencer(t: RTL8192EUTransport) -> None:
    """Port of the HW-sequencer/BAR/fast-EDCA tail (core.c:4252-4259).

    Runs after `set_tx_power` (`core.c:4242`). The `REG_FAST_EDCA_CTRL` write
    is guarded only against 8188F/8188E/8710B/8192F (core.c:4257-4258), so it
    applies to RTL8192E.
    """
    t.write8(REG_HWSEQ_CTRL, 0xFF)
    t.write32(REG_BAR_MODE_CTRL, 0x0201FFFF)
    t.write16(REG_FAST_EDCA_CTRL, 0x0000)


def set_nav_upper(t: RTL8192EUTransport) -> None:
    """Set NAV_UPPER to 30000 µs (core.c:4310-4312)."""
    val8 = ((30000 + NAV_UPPER_UNIT - 1) // NAV_UPPER_UNIT) & 0xFF
    t.write8(REG_NAV_UPPER, val8)


def disable_usb_hrpwm(t: RTL8192EUTransport) -> None:
    """Zero `REG_USB_HRPWM` on RTL8192E (core.c:4325-4327)."""
    t.write8(REG_USB_HRPWM, 0x00)


def enable_mgmt_ack(t: RTL8192EUTransport) -> None:
    """Set `FWHW_TXQ_CTRL_XMIT_MGMT_ACK` — ACK for transmitted mgmt frames
    (core.c:4329-4332). 32-bit read/modify/write in the kernel."""
    val32 = t.read32(REG_FWHW_TXQ_CTRL) | FWHW_TXQ_CTRL_XMIT_MGMT_ACK
    t.write32(REG_FWHW_TXQ_CTRL, val32 & 0xFFFFFFFF)
