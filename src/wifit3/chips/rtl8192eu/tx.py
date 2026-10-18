"""RTL8192EU TX path — raw-frame inject (MGMT, beacon and data).

Cleanroom port of:

* `struct rtl8xxxu_txdesc40` layout — `rtl8xxxu.h:414-428` (40-byte descriptor)
* `.tx_desc_size`                  — `8192e.c:1749` (= sizeof(txdesc40), no `.has_tx_report`)
* `rtl8xxxu_tx` setup tail         — `core.c:5461-5478` (pkt_size/pkt_offset, txdw0, queue)
* `rtl8xxxu_queue_select`          — `core.c:5018-5030` (BEACON/MGMT/per-AC queue)
* `rtl8xxxu_80211_to_rtl_queue`    — `core.c:4994-5015` (AC → queue, and its BE default)
* `rtl8xxxu_fill_txdesc_v2`        — `core.c:5259-5324` (MGMT and data branches)
* `rtl8xxxu_calc_tx_desc_csum`     — `core.c:5032-5053` (XOR-16 over the FIRST 32 bytes)

The 8192eu is a v2/`txdesc40` part (`.fill_txdesc = rtl8xxxu_fill_txdesc_v2`,
`8192e.c:1744`), not the 8188e's v3/`txdesc32` one. So the MGMT rate/retry bits live
in txdw3/txdw4, the 802.11 sequence number in txdw9, and the descriptor is 40 bytes.

Scope: single-MPDU injection of any frame the app hands us, at a forced rate.
Aggregation, TX-report consumption and RTS/CTS stay out of scope: a raw inject has
no sta/vif, so `ampdu_enable`/`use_rts`/`use_cts_prot` are false and
`IEEE80211_TX_CTL_ASSIGN_SEQ` is unset (`rtl8xxxu_fill_txdesc_v2`, `core.c:5268-5324`).

Wire layout of a sent URB:

    [40-byte txdesc40]  [MPDU bytes — typically 26 for a deauth]

The chip computes the actual on-air FCS; we don't append one.
"""
from __future__ import annotations

import logging
import struct

import usb.core

from .constants import (
    FC0_SUBTYPE_BEACON,
    FC0_SUBTYPE_DEAUTH,
    FC0_SUBTYPE_QOS_DATA,
    FC0_SUBTYPE_SHIFT,
    FC0_TYPE_DATA,
    FC0_TYPE_MGMT,
    FC0_TYPE_MASK,
    FC1_FROM_DS,
    FC1_TO_DS,
    IEEE80211_QOS_TID_MASK,
    IEEE80211_SEQ_SEQ_SHIFT,
    REASON_CODE_CLASS3_FRAME,
    SEQ_CTRL_OFFSET,
    TXDESC40_AGG_BREAK,
    TXDESC40_DATA_RATE_FB_MASK,
    TXDESC40_DATA_RATE_FB_SHIFT,
    TXDESC40_RETRY_LIMIT_ENABLE,
    TXDESC40_RETRY_LIMIT_SHIFT,
    TXDESC40_SEQ_SHIFT,
    TXDESC40_USE_DRIVER_RATE,
    TXDESC_BROADMULTICAST,
    TXDESC_FIRST_SEGMENT,
    TXDESC_LAST_SEGMENT,
    TXDESC_OWN,
    TXDESC_QUEUE_BE,
    TXDESC_QUEUE_BEACON,
    TXDESC_QUEUE_BK,
    TXDESC_QUEUE_MGNT,
    TXDESC_QUEUE_SHIFT,
    TXDESC_QUEUE_VI,
    TXDESC_QUEUE_VO,
)

logger = logging.getLogger(__name__)


# `sizeof(struct rtl8xxxu_txdesc40)` (rtl8xxxu.h:414-428): 4 fixed header bytes +
# txdw1..txdw6 (6×4) + csum(2) + txdw7(2) + txdw8(4) + txdw9(4) = 40. This is the
# 8192eu `.tx_desc_size` (8192e.c:1749).
TX_DESC_SZ_40 = 40

# `rtl8xxxu_calc_tx_desc_csum` XORs 16 bits at a time over the first
# `sizeof(struct rtl8xxxu_txdesc32)` = 32 bytes, even for the 8192eu's 40-byte
# txdesc40 (core.c:5032-5035, 5049).
TX_DESC_CSUM_SPAN = 32

# MGMT retry limit: `rtl8xxxu_fill_txdesc_v2` hard-codes 6 (`core.c:5300`).
TXDESC40_RETRY_LIMIT_MGNT = 6


def queue_for_frame(frame_bytes: bytes) -> int:
    """`rtl8xxxu_queue_select` (`core.c:5018-5030`) for a raw injected frame.

    Beacon → BEACON, any other MGMT → MGNT, everything else → an AC queue. The
    kernel's AC comes from `skb_get_queue_mapping()`, a mac80211 value derived from
    the frame's QoS TID (WMM / 802.11-2020 Table 10-1, mac80211
    `ieee80211_ac_from_tid` masks the TID with 7); a frame with no QoS control takes
    the kernel's `default:` case, which is BE (`core.c:5011-5013`).
    """
    if len(frame_bytes) < 2:
        raise ValueError("frame too short to carry a frame-control field")
    fc0 = frame_bytes[0]
    ftype = fc0 & FC0_TYPE_MASK
    subtype = (fc0 >> FC0_SUBTYPE_SHIFT) & 0x0F
    if ftype == FC0_TYPE_MGMT:
        return TXDESC_QUEUE_BEACON if subtype == FC0_SUBTYPE_BEACON else TXDESC_QUEUE_MGNT
    if ftype != FC0_TYPE_DATA:
        return TXDESC_QUEUE_BE
    if subtype < FC0_SUBTYPE_QOS_DATA:
        return TXDESC_QUEUE_BE

    # QoS control closes the header: after addr3 for a 2/3-address frame, after
    # addr4 for a WDS (ToDS+FromDS) one (802.11-2020 9.3.2.1.1).
    qos_offset = SEQ_CTRL_OFFSET + 2 + (
        6 if (frame_bytes[1] & FC1_TO_DS and frame_bytes[1] & FC1_FROM_DS) else 0
    )
    if len(frame_bytes) <= qos_offset:
        return TXDESC_QUEUE_BE
    tid = frame_bytes[qos_offset] & IEEE80211_QOS_TID_MASK   # 4-bit TID (9.2.4.5.4)
    return _AC_QUEUE_FOR_TID[tid & 0x07]                     # mac80211 masks with 7


# WMM access category per TID: 1,2 → BK; 0,3 → BE; 4,5 → VI; 6,7 → VO
# (802.11-2020 Table 10-1; mac80211 `ieee80211_ac_from_tid`).
_AC_QUEUE_FOR_TID = (
    TXDESC_QUEUE_BE,
    TXDESC_QUEUE_BK,
    TXDESC_QUEUE_BK,
    TXDESC_QUEUE_BE,
    TXDESC_QUEUE_VI,
    TXDESC_QUEUE_VI,
    TXDESC_QUEUE_VO,
    TXDESC_QUEUE_VO,
)


# ---- frame builders -------------------------------------------------


def build_deauth(bssid: bytes, client: bytes, reason: int = REASON_CODE_CLASS3_FRAME) -> bytes:
    """Build a 26-byte 802.11 Deauthentication frame (subtype 0xC).

    Frame layout (802.11-2020 9.3.3.13):

        | fc[2] | duration[2] | addr1=client[6] | addr2=bssid[6] | addr3=bssid[6] | seq[2] | reason[2] |

    - `addr1` is the destination (client being kicked, or `ff:..:ff` for bcast).
    - `addr2` is the source — we pretend to be the AP, so this is BSSID.
    - `addr3` is BSSID (mandatory in MGMT frames).
    - Sequence number is left at 0; fill_txdesc_v2 copies it into txdw9 unchanged
      (core.c:5289), so 0 is what goes on the wire.

    Reason codes: 0x07 (class-3 frame from non-associated STA) is a common
    "polite" disassociate that won't trigger anti-deauth heuristics on
    well-behaved clients but still kicks the session.
    """
    if len(bssid) != 6:
        raise ValueError(f"bssid must be 6 bytes, got {len(bssid)}")
    if len(client) != 6:
        raise ValueError(f"client must be 6 bytes, got {len(client)}")
    fc0 = FC0_TYPE_MGMT | FC0_SUBTYPE_DEAUTH
    return struct.pack(
        "<BBH6s6s6sHH",
        fc0,           # fc[0]
        0x00,          # fc[1]: ToDS=0, FromDS=0, no flags
        0x013A,        # Duration: 314 µs (typical mgmt frame duration value)
        client,        # addr1: destination
        bssid,         # addr2: source (we spoof the AP)
        bssid,         # addr3: BSSID
        0,             # seq_ctrl (fill_txdesc_v2 → txdw9)
        reason,        # body: reason code
    )


# ---- descriptor builder ---------------------------------------------


def build_tx_desc(frame_bytes: bytes, queue: int, is_broadcast: bool,
                  retry_limit: int = TXDESC40_RETRY_LIMIT_MGNT) -> bytearray:
    """Construct a 40-byte txdesc40 for an injected frame.

    Mirrors `rtl8xxxu_tx` (core.c:5461-5478) for the common header + queue, and
    `rtl8xxxu_fill_txdesc_v2` (core.c:5259-5324) branch by branch: MGMT and beacon
    frames take the MGMT branch (forced rate + retry limit, core.c:5294-5302), data
    frames take the data branch (rate-fallback field, no forced rate, core.c:5283-5285).
    `queue` is `queue_for_frame`'s value — for a beacon the caller passes
    `TXDESC_QUEUE_BEACON`, which is also the queue the descriptor routes to
    (core.c:5022-5023). Checksum is NOT computed here — call `calc_tx_desc_csum` on the
    finished descriptor.

    A raw inject has no sta/vif, so `ampdu_enable`/`use_rts`/`use_cts_prot` are all
    false and `rts_rate` is 0: the RTS/CTS/self-CTS block (core.c:5310-5324) and the
    AMPDU-density bits are inert. Likewise IEEE80211_TX_CTL_ASSIGN_SEQ is unset, so
    txdw8's HW_SEQ_ENABLE (core.c:5304-5305) stays clear. The frame's own sequence
    number goes into txdw9 (core.c:5281, 5289); a frame too short to carry the header
    sends sequence number 0, as the kernel's zeroed descriptor would.
    """
    desc = bytearray(TX_DESC_SZ_40)

    # Common header:
    #   pkt_size   = MPDU length        (bytes 0-1, LE u16 — core.c:5464)
    #   pkt_offset = tx_desc_size       (byte 2 — core.c:5465)
    #   txdw0      = OWN | FIRST_SEG | LAST_SEG (byte 3 — core.c:5469-5470)
    struct.pack_into("<H", desc, 0, len(frame_bytes))
    desc[2] = TX_DESC_SZ_40
    txdw0 = TXDESC_OWN | TXDESC_FIRST_SEGMENT | TXDESC_LAST_SEGMENT
    if is_broadcast:
        txdw0 |= TXDESC_BROADMULTICAST  # core.c:5471-5474
    desc[3] = txdw0

    # txdw1: queue in bits[12:8] (core.c:5478 with `rtl8xxxu_queue_select`,
    # core.c:5018-5030). MACID stays 0: fill_txdesc_v2 ORs `macid << TXDESC40_MACID_SHIFT`
    # (core.c:5279) but rtl8xxxu_get_macid() returns 0 when there is no sta (core.c:3908-3909).
    txdw1 = queue << TXDESC_QUEUE_SHIFT
    struct.pack_into("<I", desc, 4, txdw1)

    # txdw2: AGG_BREAK — ampdu_enable is false, so the else branch is taken
    # (core.c:5291-5294).
    struct.pack_into("<I", desc, 8, TXDESC40_AGG_BREAK)

    ftype = (frame_bytes[0] & FC0_TYPE_MASK) if frame_bytes else None

    # txdw3: USE_DRIVER_RATE for MGMT (core.c:5298); rts_rate is 0 and use_rts/
    # use_cts_prot are false, so no RTS_CTS/CTS_SELF/HW_RTS bits (core.c:5310-5324).
    txdw3 = TXDESC40_USE_DRIVER_RATE if ftype == FC0_TYPE_MGMT else 0
    struct.pack_into("<I", desc, 12, txdw3)

    # txdw4: rate = 0 (core.c:5283). DATA gets the rate-fallback field (core.c:5284-5286);
    # the MGMT branch reassigns the word and adds the retry limit + enable bit
    # (core.c:5296-5301). Control frames keep txdw4 = rate = 0. RTS_RATE occupies
    # bits[29:24] (core.c:5310) and is 0 here.
    txdw4 = 0
    if ftype == FC0_TYPE_DATA:
        txdw4 = TXDESC40_DATA_RATE_FB_MASK << TXDESC40_DATA_RATE_FB_SHIFT
    elif ftype == FC0_TYPE_MGMT:
        txdw4 = (
            ((retry_limit & 0x3F) << TXDESC40_RETRY_LIMIT_SHIFT)  # 6-bit field, rtl8xxxu.h:567
            | TXDESC40_RETRY_LIMIT_ENABLE
        )
    struct.pack_into("<I", desc, 16, txdw4)

    # txdw5: 0 — SHORT_PREAMBLE is only set for a connected sta/vif (core.c:5307-5308).
    # txdw6: 0 (unused on this path).
    # csum at bytes 28-29 — left 0 here, filled by calc_tx_desc_csum.
    # txdw7: 0 — v2 sets no antenna bits (unlike fill_txdesc_v3, core.c:5407-5409).
    # txdw8: 0 — HW_SEQ_ENABLE needs IEEE80211_TX_CTL_ASSIGN_SEQ (core.c:5304-5305).

    # txdw9: seq_number << TXDESC40_SEQ_SHIFT (core.c:5281, 5289). seq_number is
    # `IEEE80211_SEQ_TO_SN(seq_ctrl)` — the header's 12-bit sequence field.
    # txdw9 is the last word of the 40-byte descriptor (offset 36).
    if len(frame_bytes) >= SEQ_CTRL_OFFSET + 2:
        seq_ctrl = int.from_bytes(frame_bytes[SEQ_CTRL_OFFSET : SEQ_CTRL_OFFSET + 2], "little")
        seq_number = seq_ctrl >> IEEE80211_SEQ_SEQ_SHIFT
        struct.pack_into("<I", desc, 36, seq_number << TXDESC40_SEQ_SHIFT)

    return desc


def calc_tx_desc_csum(desc: bytearray) -> None:
    """Port of `rtl8xxxu_calc_tx_desc_csum` (core.c:5037-5053).

    XOR-16 over the FIRST `sizeof(struct rtl8xxxu_txdesc32)` = 32 bytes of the
    descriptor, with the `csum` field (bytes 28-29) cleared first, result stored
    back into csum. The 40-byte txdesc40 is still only checksummed over its first
    32 bytes (core.c:5032-5035, 5049).
    """
    desc[28] = 0
    desc[29] = 0
    csum = 0
    for i in range(0, TX_DESC_CSUM_SPAN, 2):
        csum ^= int.from_bytes(desc[i : i + 2], "little")
    csum &= 0xFFFF
    desc[28] = csum & 0xFF
    desc[29] = (csum >> 8) & 0xFF


# ---- bulk-OUT send --------------------------------------------------


def send_frame(
    dev: usb.core.Device,
    ep_out: int,
    mpdu: bytes,
    *,
    queue: int,
    is_broadcast: bool = False,
    retry_limit: int = TXDESC40_RETRY_LIMIT_MGNT,
    timeout_ms: int = 200,
) -> int:
    """Send one frame on `ep_out`: build the descriptor for `queue`, checksum, bulk-OUT.

    `queue` comes from `queue_for_frame` and must match the endpoint `ep_out` was chosen
    for (`mac.endpoint_index_for_queue`), as in the kernel's `priv->pipe_out[queue]`
    (core.c:5550). `retry_limit` only applies to the MGMT branch of the descriptor.
    Returns the number of bytes actually written (descriptor + MPDU). Raises
    `usb.core.USBError` on USB-level failure (e.g. timeout, pipe stall, no device).
    """
    desc = build_tx_desc(mpdu, queue, is_broadcast, retry_limit=retry_limit)
    calc_tx_desc_csum(desc)
    urb = bytes(desc) + mpdu
    written = dev.write(ep_out, urb, timeout_ms)
    if written != len(urb):
        raise IOError(
            f"bulk-OUT short write: sent {written}, expected {len(urb)}"
        )
    return written
