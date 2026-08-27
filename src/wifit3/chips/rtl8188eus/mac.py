"""RTL8188EUS post-FW MAC init + LLT setup.

Cleanroom port of the post-`start_firmware` MAC bring-up path from
`driver_sources/rtl8xxxu-source-v6.18/`:

* `rtl8xxxu_init_mac`              — `core.c:2187-2226` (mactable + REG_MAX_AGGR_NUM)
* `rtl8xxxu_init_queue_reserved_page` — `core.c:3815-3845` (TX FIFO partition)
* TX buffer boundary block         — `core.c:4023-4031` (TXPKTBUF_* + TRXFF_BNDY byte)
* REG_PBP                          — `core.c:4038-4041`
* `rtl8xxxu_init_llt_table`        — `core.c:2519-2556`
* `rtl8xxxu_llt_write`             — `core.c:2498-2517`
* MAC TX/RX enable flip            — `8188e.c:1299-1301` (inside `rtl8188e_usb_quirks`)

The kernel writes `REG_TRXFF_BNDY + 2 = 0x25ff` (RX page boundary) BEFORE
the FW upload (`core.c:3962`). We perform it post-FW in `post_fw_mac_init`
instead — M1 already passed without that write, proving the 8051 boot
path doesn't need it. The comment in `8188e.c:1180-1183` only mandates
that `REG_TRXFF_BNDY` is set BEFORE flipping `MAC_TX/RX_ENABLE`, which
this module honours.
"""
from __future__ import annotations

import logging

from .constants import (
    CR_MAC_RX_ENABLE,
    CR_MAC_TX_ENABLE,
    GPIO_MUXCFG_IO_SEL_ENBT,
    MCU_WINT_INIT_READY,
    HIMR0_8188E,
    HIMR1_8188E,
    LAST_LLT_ENTRY_8188E,
    LLT_OP_INACTIVE,
    LLT_OP_MASK,
    LLT_OP_WRITE,
    LLT_WRITE_POLL_MAX,
    MAX_AGGR_NUM_8188E,
    PAGE_NUM_HI_PQ_8188E,
    PAGE_NUM_LO_PQ_8188E,
    PAGE_NUM_NORM_PQ_8188E,
    PBP_PAGE_SIZE_128,
    PBP_PAGE_SIZE_RX_SHIFT,
    PBP_PAGE_SIZE_TX_SHIFT,
    RCR_MONITOR,
    REG_CR,
    REG_TRXDMA_CTRL,
    TRXDMA_CTRL_BEQ_SHIFT,
    TRXDMA_CTRL_BKQ_SHIFT,
    TRXDMA_CTRL_HIQ_SHIFT,
    TRXDMA_CTRL_MGQ_SHIFT,
    TRXDMA_CTRL_VIQ_SHIFT,
    TRXDMA_CTRL_VOQ_SHIFT,
    TRXDMA_QUEUE_HIGH,
    TRXDMA_QUEUE_NORMAL,
    REG_GPIO_MUXCFG,
    REG_HIMR0,
    REG_HIMR1,
    REG_HISR0,
    REG_LLT_INIT,
    REG_MAX_AGGR_NUM,
    REG_MCU_FW_DL,
    REG_PBP,
    REG_RCR,
    REG_RQPN,
    REG_RQPN_NPQ,
    REG_RX_DRVINFO_SZ,
    REG_RXFLTMAP1,
    REG_TDECTRL,
    REG_TRXFF_BNDY,
    REG_TXPKTBUF_BCNQ_BDNY,
    REG_TXPKTBUF_MGQ_BDNY,
    REG_TXPKTBUF_WMAC_LBK_BF_HD,
    REG_USB_SPECIAL_OPTION,
    RQPN_EPQ_SHIFT,
    RQPN_HI_PQ_SHIFT,
    RQPN_LO_PQ_SHIFT,
    RQPN_LOAD,
    RQPN_NPQ_SHIFT,
    RQPN_PUB_PQ_SHIFT,
    TOTAL_PAGE_NUM_8188E,
    TRXFF_BOUNDARY_8188E,
    USB_SPEC_INT_BULK_SELECT,
)
from .transport import RTL8188EUSTransport

logger = logging.getLogger(__name__)


# 8188e MAC init table — `8188e.c:19-44`, 78 (reg, val) byte writes
# terminated by the (0xffff, 0xff) sentinel.
MAC_INIT_TABLE_8188E: tuple[tuple[int, int], ...] = (
    (0x026, 0x41), (0x027, 0x35), (0x040, 0x00), (0x421, 0x0F),
    (0x428, 0x0A), (0x429, 0x10), (0x430, 0x00), (0x431, 0x01),
    (0x432, 0x02), (0x433, 0x04), (0x434, 0x05), (0x435, 0x06),
    (0x436, 0x07), (0x437, 0x08), (0x438, 0x00), (0x439, 0x00),
    (0x43A, 0x01), (0x43B, 0x02), (0x43C, 0x04), (0x43D, 0x05),
    (0x43E, 0x06), (0x43F, 0x07), (0x440, 0x5D), (0x441, 0x01),
    (0x442, 0x00), (0x444, 0x15), (0x445, 0xF0), (0x446, 0x0F),
    (0x447, 0x00), (0x458, 0x41), (0x459, 0xA8), (0x45A, 0x72),
    (0x45B, 0xB9), (0x460, 0x66), (0x461, 0x66), (0x480, 0x08),
    (0x4C8, 0xFF), (0x4C9, 0x08), (0x4CC, 0xFF), (0x4CD, 0xFF),
    (0x4CE, 0x01), (0x4D3, 0x01), (0x500, 0x26), (0x501, 0xA2),
    (0x502, 0x2F), (0x503, 0x00), (0x504, 0x28), (0x505, 0xA3),
    (0x506, 0x5E), (0x507, 0x00), (0x508, 0x2B), (0x509, 0xA4),
    (0x50A, 0x5E), (0x50B, 0x00), (0x50C, 0x4F), (0x50D, 0xA4),
    (0x50E, 0x00), (0x50F, 0x00), (0x512, 0x1C), (0x514, 0x0A),
    (0x516, 0x0A), (0x525, 0x4F), (0x550, 0x10), (0x551, 0x10),
    (0x559, 0x02), (0x55D, 0xFF), (0x605, 0x30), (0x608, 0x0E),
    (0x609, 0x2A), (0x620, 0xFF), (0x621, 0xFF), (0x622, 0xFF),
    (0x623, 0xFF), (0x624, 0xFF), (0x625, 0xFF), (0x626, 0xFF),
    (0x627, 0xFF), (0x63C, 0x08), (0x63D, 0x08), (0x63E, 0x0C),
    (0x63F, 0x0C), (0x640, 0x40), (0x652, 0x20), (0x66E, 0x05),
    (0x700, 0x21), (0x701, 0x43), (0x702, 0x65), (0x703, 0x87),
    (0x708, 0x21), (0x709, 0x43), (0x70A, 0x65), (0x70B, 0x87),
)


def apply_mac_init_table(t: RTL8188EUSTransport) -> int:
    """Port of `rtl8xxxu_init_mac` (core.c:2187-2226).

    Iterates the MAC init table, then applies the 8188E `REG_MAX_AGGR_NUM
    = 0x0707` branch (core.c:2218-2220). Returns the number of byte writes.
    """
    count = 0
    for reg, val in MAC_INIT_TABLE_8188E:
        t.write8(reg, val)
        count += 1
    t.write16(REG_MAX_AGGR_NUM, MAX_AGGR_NUM_8188E)
    return count


def init_queue_reserved_page(
    t: RTL8188EUSTransport,
    has_high_queue: bool = False,
    has_low_queue: bool = False,
    has_normal_queue: bool = True,
) -> None:
    """Port of `rtl8xxxu_init_queue_reserved_page` (core.c:3815-3845).

    Partitions the TX page FIFO into HI/LO/NORM/PUB queues. Defaults
    assume the typical 1-TX-endpoint case (normal queue only); flip the
    flags when later milestones need additional queues.
    """
    hq = PAGE_NUM_HI_PQ_8188E if has_high_queue else 0
    lq = PAGE_NUM_LO_PQ_8188E if has_low_queue else 0
    nq = PAGE_NUM_NORM_PQ_8188E if has_normal_queue else 0
    eq = 0

    val32 = (nq << RQPN_NPQ_SHIFT) | (eq << RQPN_EPQ_SHIFT)
    t.write32(REG_RQPN_NPQ, val32)

    pubq = TOTAL_PAGE_NUM_8188E - hq - lq - nq - 1
    val32 = (
        RQPN_LOAD
        | (hq << RQPN_HI_PQ_SHIFT)
        | (lq << RQPN_LO_PQ_SHIFT)
        | (pubq << RQPN_PUB_PQ_SHIFT)
    )
    t.write32(REG_RQPN, val32)


def set_trxff_rx_page_boundary(t: RTL8188EUSTransport) -> None:
    """Write the RX page boundary (REG_TRXFF_BNDY+2 = 0x25ff).

    Kernel does this PRE-FW at `core.c:3962`; we hoist it here because M1
    already finished FW upload without it. Required before `MAC_TX_ENABLE
    | MAC_RX_ENABLE` per the 88E HW bug comment at `8188e.c:1180-1183`.
    """
    t.write16(REG_TRXFF_BNDY + 2, TRXFF_BOUNDARY_8188E)


def set_tx_buffer_boundary(t: RTL8188EUSTransport) -> None:
    """Port of `core.c:4023-4031` — TX buffer boundary 5-write block."""
    val8 = (TOTAL_PAGE_NUM_8188E + 1) & 0xFF  # 0xAA
    t.write8(REG_TXPKTBUF_BCNQ_BDNY, val8)
    t.write8(REG_TXPKTBUF_MGQ_BDNY, val8)
    t.write8(REG_TXPKTBUF_WMAC_LBK_BF_HD, val8)
    t.write8(REG_TRXFF_BNDY, val8)
    t.write8(REG_TDECTRL + 1, val8)


def set_pbp(t: RTL8188EUSTransport) -> None:
    """Port of `core.c:4038-4041` — REG_PBP (page boundary partition).

    8188e uses 128-byte pages for both RX and TX (`.pbp_rx`/`.pbp_tx =
    PBP_PAGE_SIZE_128` in `8188e.c:1877-1878`).
    """
    val8 = (PBP_PAGE_SIZE_128 << PBP_PAGE_SIZE_RX_SHIFT) | (
        PBP_PAGE_SIZE_128 << PBP_PAGE_SIZE_TX_SHIFT
    )
    t.write8(REG_PBP, val8)


def _llt_write(t: RTL8188EUSTransport, address: int, data: int) -> None:
    """Port of `rtl8xxxu_llt_write` (core.c:2498-2517).

    Writes one LLT entry by encoding `LLT_OP_WRITE | (address << 8) |
    data` into REG_LLT_INIT, then polling until the chip self-clears the
    op-code to LLT_OP_INACTIVE.
    """
    cmd = LLT_OP_WRITE | (address << 8) | (data & 0xFF)
    t.write32(REG_LLT_INIT, cmd)
    for _ in range(LLT_WRITE_POLL_MAX):
        v = t.read32(REG_LLT_INIT)
        if (v & LLT_OP_MASK) == LLT_OP_INACTIVE:
            return
    raise IOError(
        f"LLT write to entry {address:#x} did not return to INACTIVE "
        f"within {LLT_WRITE_POLL_MAX} polls"
    )


def init_llt_table(t: RTL8188EUSTransport) -> None:
    """Port of `rtl8xxxu_init_llt_table` (core.c:2519-2556).

    For 8188e: total_page_num=0xa9 (TX pages 0..168 chained 0→1→...→168),
    page 169 marks TX end (0xff), pages 170..174 form a 5-page ring
    buffer, and entry 175 loops back to 170. 176 LLT writes total.
    """
    total = TOTAL_PAGE_NUM_8188E
    last = LAST_LLT_ENTRY_8188E

    for i in range(total):
        _llt_write(t, i, i + 1)
    _llt_write(t, total, 0xFF)
    for i in range(total + 1, last):
        _llt_write(t, i, i + 1)
    _llt_write(t, last, total + 1)


def enable_mac_tx_rx(t: RTL8188EUSTransport) -> None:
    """Port of the MAC enable flip from `rtl8188e_usb_quirks`
    (`8188e.c:1299-1301`). Sets `CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE` —
    the bits we held off in M1's `power_on` per the 88E TRXFF_BNDY HW
    bug. By this point both REG_TRXFF_BNDY writes have happened, so the
    workaround precondition is satisfied.
    """
    val16 = t.read16(REG_CR)
    val16 |= CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE
    t.write16(REG_CR, val16)


def is_chip_warm(t: RTL8188EUSTransport) -> bool:
    """Return True if the chip looks like a previous wifit3 session
    left it FW-running + MAC-enabled.

    Two signals:
      1. `MCU_WINT_INIT_READY` (bit 6 of REG_MCU_FW_DL) — set by
         `start_firmware` once the 8051 boots the uploaded FW image.
      2. `CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE` (bits 6+7 of REG_CR) —
         set at the tail of `post_fw_mac_init`.

    Both bits together mean we're past M1+M2 of the bring-up, so we can
    skip the FW upload and MAC/PHY init and just resume RX/TX.

    Returns False on any USB read error — safer to do a full cold boot
    than to mistakenly skip init steps the chip actually needs.
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


def init_queue_priority_2ep(t: RTL8188EUSTransport) -> None:
    """Port of the 2-bulk-OUT case from `rtl8xxxu_init_queue_priority`
    (core.c:2618-2647 + 2683-2693).

    The 8188EUS exposes 2 bulk-OUT endpoints (EP 0x02 + EP 0x03). The
    kernel auto-detects which is HIGH-priority and which is NORMAL from
    the USB descriptor's vendor-specific extra bytes; without that
    descriptor parse we hard-pick the conventional mapping: EP 0x02 (the
    lower-numbered endpoint = first in PyUSB's `bulk_out` list) is the
    HIGH queue, EP 0x03 is NORMAL. With the kernel's 2-EP routing
    convention this puts:

        VO/VI/MGMT/HIGH  →  HIGH lane  →  EP 0x02 (first bulk-OUT)
        BE/BK            →  NORMAL lane → EP 0x03 (second bulk-OUT)

    Writes only `REG_TRXDMA_CTRL`; the kernel's `priv->pipe_out[...]`
    mapping array is replaced by `tx.pick_bulk_out_mgmt()` at TX time.
    """
    hi = TRXDMA_QUEUE_HIGH
    lo = TRXDMA_QUEUE_NORMAL
    val16 = t.read16(REG_TRXDMA_CTRL) & 0x7  # preserve bottom 3 bits
    val16 |= (
        (hi << TRXDMA_CTRL_VOQ_SHIFT)
        | (hi << TRXDMA_CTRL_VIQ_SHIFT)
        | (lo << TRXDMA_CTRL_BEQ_SHIFT)
        | (lo << TRXDMA_CTRL_BKQ_SHIFT)
        | (hi << TRXDMA_CTRL_MGQ_SHIFT)
        | (hi << TRXDMA_CTRL_HIQ_SHIFT)
    )
    t.write16(REG_TRXDMA_CTRL, val16 & 0xFFFF)


def apply_monitor_rx_filter(t: RTL8188EUSTransport) -> None:
    """Reassert the monitor RCR. Called on BOTH cold + warm attach.

    `enable_rx_data_path` writes RCR_MONITOR (incl. RCR_ACCEPT_AP/promiscuous),
    but it only runs on the cold path. The warm path skips it, so a chip left
    by a prior session keeps a non-promiscuous RCR and drops client→AP (ToDS)
    frames — incl. M2/M4 EAPOL (only M1/M3 seen, HW-confirmed 2026-05-25).
    Reassert it here — the interrupts/DRVINFO `enable_rx_data_path` also sets
    persist on a warm chip, so only the RCR (the direction filter) needs
    re-writing. RCR_MONITOR already includes RCR_APPEND_PHYSTAT, so RSSI is
    unaffected. Mirrors the rtl8821au/rtl8822bu fix.
    """
    t.write32(REG_RCR, RCR_MONITOR)
    rcr = t.read32(REG_RCR)
    logger.debug("RX filter readback: RCR=0x%08x (ACCEPT_AP=%d)",
                rcr, 1 if rcr & 0x1 else 0)


# ACK is ctrl subtype 13; RXFLTMAP1 bit N gates ctrl subtype N. The 8188e fileops never
# programs RXFLTMAP (it's left at the HW default), so admit bit13 explicitly on demand so
# monitor RX sees the AP's ACK to our injects. RCR already has ACCEPT_CTRL_FRAME.
RXFLTMAP1_ACK = 1 << 13


def admit_ack_frames(t: RTL8188EUSTransport) -> None:
    """RXFLTMAP1 |= BIT(13) — let RX see the AP's ACKs to our injects. Off by default."""
    t.write16(REG_RXFLTMAP1, t.read16(REG_RXFLTMAP1) | RXFLTMAP1_ACK)


def drop_ack_frames(t: RTL8188EUSTransport) -> None:
    """Clear RXFLTMAP1 BIT(13) — restore the default monitor ctrl filter."""
    t.write16(REG_RXFLTMAP1, t.read16(REG_RXFLTMAP1) & ~RXFLTMAP1_ACK)


def enable_rx_data_path(t: RTL8188EUSTransport) -> None:
    """Port of the post-PHY RX-enable writes from `core.c:4100-4154`
    (universal block + the 8188E branch at 4108-4116).

    After this returns, the chip is configured to accept management and
    data frames and the bulk-IN endpoint will start delivering RX URBs.
    The RCR write is the firehose-open: with the right bits set the chip
    accepts management, multicast, broadcast, and ACCEPT_PHYS_MATCH frames
    + appends PHY status / ICV / MIC to each delivered MPDU.

    Skipped: REG_RXFLTMAP* writes — `fops->init_reg_rxfltmap` is not set
    on the 8188e fileops vector (`8188e.c:1835-1885`), so we follow the
    ELSE branch (`core.c:4148-4154`) which writes the multicast filter
    instead. For monitor-mode RX the default REG_RXFLTMAP* are fine.
    """
    # Universal: PHY stats appended to each RX, 4 × 8 bytes = 32 B.
    t.write8(REG_RX_DRVINFO_SZ, 4)

    # 8188E-specific interrupt config + USB special option (core.c:4108-4116).
    t.write32(REG_HISR0, 0xFFFFFFFF)             # clear pending interrupts
    t.write32(REG_HIMR0, HIMR0_8188E)
    t.write32(REG_HIMR1, HIMR1_8188E)
    t.write8(REG_USB_SPECIAL_OPTION, t.read8(REG_USB_SPECIAL_OPTION) | USB_SPEC_INT_BULK_SELECT)

    # The firehose: REG_RCR (core.c:4130-4133).
    t.write32(REG_RCR, RCR_MONITOR)

    # 8188e GPIO mux: clear BT-coex enable bit (core.c:4281-4283 8188e branch).
    val8 = t.read8(REG_GPIO_MUXCFG)
    val8 &= ~GPIO_MUXCFG_IO_SEL_ENBT & 0xFF
    t.write8(REG_GPIO_MUXCFG, val8)


def post_fw_mac_init(t: RTL8188EUSTransport) -> None:
    """Run the full M2 post-FW MAC init sequence end-to-end.

    Pass-line: ``REG_CR`` has ``CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE`` set
    on return. ``REG_TRXFF_BNDY`` writes happen before the MAC enable
    flip per the 88E HW bug.
    """
    logger.debug("applying 8188e MAC init table (78 byte writes + MAX_AGGR_NUM)")
    apply_mac_init_table(t)

    logger.debug("init_queue_reserved_page (normal queue only)")
    init_queue_reserved_page(t)

    logger.debug("init_queue_priority (2-bulk-OUT: HIGH=EP0x02, NORMAL=EP0x03)")
    init_queue_priority_2ep(t)

    logger.debug("set REG_TRXFF_BNDY+2 = 0x%04x (RX page boundary)", TRXFF_BOUNDARY_8188E)
    set_trxff_rx_page_boundary(t)

    logger.debug("set TX buffer boundary block (REG_TRXFF_BNDY = 0x%02x)", TOTAL_PAGE_NUM_8188E + 1)
    set_tx_buffer_boundary(t)

    logger.debug("set REG_PBP (page size 128 for RX and TX)")
    set_pbp(t)

    logger.debug("init_llt_table (176 polled LLT writes)")
    init_llt_table(t)

    logger.debug("flip CR_MAC_TX_ENABLE | CR_MAC_RX_ENABLE in REG_CR")
    enable_mac_tx_rx(t)
