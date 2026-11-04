"""rt2800usb TX path: rt2800usb_write_tx_desc (rt2800usb.c:401-435) + rt2800_write_tx_data (rt2800lib.c:795-853).

Wire layout (rt2800usb_get_tx_data_len, rt2800usb.c:440-451):

    [TXINFO 4B] [TXWI 16B, 20B on RT5592] [802.11 frame] [4-byte align pad] [4-byte USB end pad]
"""
from __future__ import annotations

import logging
import struct

import usb.core

from wifit3.chips import log_trace

from .constants import (
    IEEE80211_FCTL_FROMDS,
    IEEE80211_FCTL_FTYPE,
    IEEE80211_FCTL_MOREFRAGS,
    IEEE80211_FCTL_ORDER,
    IEEE80211_FCTL_STYPE,
    IEEE80211_FCTL_TODS,
    IEEE80211_FTYPE_CTL,
    IEEE80211_FTYPE_DATA,
    IEEE80211_FTYPE_EXT,
    IEEE80211_FTYPE_MGMT,
    IEEE80211_HT_CTL_LEN,
    IEEE80211_QOS_CTL_LEN,
    IEEE80211_STYPE_CTS,
    IEEE80211_STYPE_RTS,
    IEEE80211_STYPE_QOS_DATA,
    IEEE802_1D_TO_AC,
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
    TXWI_W0_FRAG,
    TXWI_W0_MCS,
    TXWI_W0_PHYMODE,
    TXWI_W0_TX_OP,
    TXWI_W1_ACK,
    TXWI_W1_MPDU_TOTAL_BYTE_COUNT,
    TXWI_W1_NSEQ,
    TXWI_W1_PACKETID_ENTRY,
    TXWI_W1_PACKETID_QUEUE,
    TXWI_W1_WIRELESS_CLI_ID,
)

QSEL_EDCA = 2    # rt2800usb_write_tx_desc [SRC rt2800usb.c:422]

logger = logging.getLogger(__name__)


def txwi_size_for_silicon(silicon_id: int) -> int:
    """RT5592 uses 5-word TXWI (20 B); everything else uses 4-word (16 B)."""
    if silicon_id == RT_RT5592:
        return TXWI_DESC_SIZE_5WORDS
    return TXWI_DESC_SIZE_4WORDS


def ieee80211_hdrlen(frame_control: int) -> int:
    """802.11 header length mac80211 requires of an injected frame [SRC net/wireless/util.c:430-473]."""
    ftype = frame_control & IEEE80211_FCTL_FTYPE
    if ftype == IEEE80211_FTYPE_EXT:
        return 4
    hdrlen = 24
    if ftype == IEEE80211_FTYPE_DATA:
        a4 = IEEE80211_FCTL_TODS | IEEE80211_FCTL_FROMDS
        if (frame_control & a4) == a4:                # ieee80211_has_a4
            hdrlen = 30
        if frame_control & IEEE80211_STYPE_QOS_DATA:
            hdrlen += IEEE80211_QOS_CTL_LEN
            if frame_control & IEEE80211_FCTL_ORDER:
                hdrlen += IEEE80211_HT_CTL_LEN
    elif ftype == IEEE80211_FTYPE_MGMT:
        if frame_control & IEEE80211_FCTL_ORDER:
            hdrlen += IEEE80211_HT_CTL_LEN
    elif (frame_control & 0x00E0) == 0x00C0:          # ACK, CTS
        hdrlen = 10
    else:
        hdrlen = 16
    return hdrlen


def ieee80211_get_qos_ctl(frame: bytes) -> int:
    """Offset of a QoS data frame's QoS control field [SRC include/linux/ieee80211.h:4540-4552]."""
    if (frame[1] & 0x03) == 0x03:                     # ieee80211_has_a4
        return 30
    return 24


def select_queue(frame: bytes) -> int:
    """The AC queue mac80211 puts an injected frame on [SRC net/mac80211/tx.c ieee80211_monitor_start_xmit,
    wme.c ieee80211_select_queue_80211]; a monitor vif has no ACM bits, so ieee80211_downgrade_queue keeps it."""
    fc0 = frame[0]
    if (fc0 & 0x0C) != 0x08:                          # not a data frame: priority 7
        return IEEE802_1D_TO_AC[7]
    if not (fc0 & 0x80):                              # data without QoS: priority 0
        return IEEE802_1D_TO_AC[0]
    return IEEE802_1D_TO_AC[frame[ieee80211_get_qos_ctl(frame)] & 0x07]    # IEEE80211_QOS_CTL_TAG1D_MASK


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
    packetid_queue: int,
    packetid_entry: int,
    next_valid: int = 0,
    tx_burst: int = 0,
    frag: int = 0,
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
    txwi_w0 = _set_field32(txwi_w0, TXWI_W0_FRAG, frag)          # [SRC] rt2800lib.c:806-807
    txwi_w0 = _set_field32(txwi_w0, TXWI_W0_MCS, mcs)
    txwi_w0 = _set_field32(txwi_w0, TXWI_W0_PHYMODE, phymode)
    txwi_w0 = _set_field32(txwi_w0, TXWI_W0_TX_OP, TXWI_TX_OP_NONE)
    # MIMO_PS/AMPDU/BW/SHORT_GI/STBC/IFS/FRAG/CF_ACK/TS = 0

    # TXWI_W1 [SRC rt2800lib.c:827-839]
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
    ep: int,
    qid: int,
    entry_index: int,
    use_no_ack: bool = True,
    mcs: int = 0,
    phymode: int = TXWI_PHYMODE_CCK,
    timeout_ms: int = 1000,
) -> int:
    """Build TX descriptors for TX queue ``qid`` entry ``entry_index`` + pad-align frame + bulk-OUT write on ``ep``.

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
    if len(frame) < 2:
        raise ValueError("frame is shorter than its frame control field")
    if len(frame) > 0x0FFF:
        raise ValueError(f"frame too long for TXWI MPDU field ({len(frame)} > 4095)")

    # ENTRY_TXD_MORE_FRAG / ENTRY_TXD_BURST [SRC rt2x00queue.c:409-434 rt2x00queue_create_tx_descriptor]
    frame_control = struct.unpack_from("<H", frame)[0]
    more_frag = bool(frame_control & IEEE80211_FCTL_MOREFRAGS)
    burst = more_frag
    if (frame_control & IEEE80211_FCTL_FTYPE) == IEEE80211_FTYPE_CTL:
        subtype = frame_control & IEEE80211_FCTL_STYPE
        if subtype == IEEE80211_STYPE_RTS or subtype == IEEE80211_STYPE_CTS:
            burst = True
    prefix = build_tx_descriptors(
        len(frame), txwi_size,
        use_no_ack=use_no_ack, mcs=mcs, phymode=phymode,
        packetid_queue=qid, packetid_entry=(entry_index % 3) + 1,      # [SRC] rt2800lib.c:838-839
        tx_burst=int(burst), frag=int(more_frag),                      # [SRC] rt2800usb.c:425-426
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
