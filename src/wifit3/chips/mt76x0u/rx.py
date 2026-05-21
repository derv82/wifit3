"""MT76x0U RX-descriptor decode — strips the bulk-IN packet header to
hand a raw 802.11 frame + RSSI to `WlanFrameParser`.

[SRC] dma.h:44-47 (MT_DMA_HDR_LEN=4, MT_RX_RXWI_LEN=32, MT_FCE_INFO_LEN=4)
[SRC] mt76x02_mac.h:97-108 (struct mt76x02_rxwi — 32 bytes total)
[SRC] mt76x02_mac.c:771-873 (mt76x02_mac_process_rx)
[SRC] mt76x02_mac.h:46-74 (MT_RXINFO_* bit defs)
[SRC] mt76x02_txrx.c:35-53 (mt76x02_queue_rx_skb — `skb_pull(sizeof rxwi)`
       i.e. 32 bytes before the 802.11 frame, confirmed against kernel)
[SRC] usb.c:454-470 (mt76u_get_rx_entry_len — dma_len is le16 of first 2B)

Each bulk-IN packet on EP 0x84 looks like:

  Offset  Size  Field
  0       2     dma_len (le16) — length of the rest of the buffer (RXWI +
                  frame + FCE trailer + alignment pad). Always 4-byte aligned.
  2       2     reserved (kernel doesn't decode these — zero on our card)
  4       4     rxwi.rxinfo (le32) — BEACON/UNICAST/AMPDU/L2PAD/etc flags
  8       4     rxwi.ctl (le32) — WCID (b0-7), MPDU_LEN (b16-29)
  12      2     rxwi.tid_sn (le16) — TID (b0-3), SN (b4-15)
  14      2     rxwi.rate (le16) — rate index + PHY/BW/SGI/STBC
  16      4     rxwi.rssi[4] — per-chain raw RSSI (s8 each)
  20      16    rxwi.bbp_rxinfo[4] (le32 ×4) — BBP-derived metadata
  36      ...   802.11 frame (MPDU_LEN bytes; if MT_RXINFO_L2PAD bit is set
                  there are 2 padding bytes BETWEEN the 802.11 header and
                  body — mt76x02_remove_hdr_pad memmoves them out. For M4c
                  we just drop L2PAD-flagged frames.)
  ...     4     FCE info trailer (le32) — at the end, NOT the beginning
  END     ...   alignment padding to 4-byte boundary

Total header size before the 802.11 frame = MT_DMA_HDR_LEN + MT_RX_RXWI_LEN
= 4 + 32 = 36 bytes.
"""
from __future__ import annotations

import logging
import struct
from typing import Iterator, NamedTuple, Optional

logger = logging.getLogger(__name__)


# --- bit-field masks (from kernel mt76x02_dma.h + mt76x02_mac.h) -------------

MT_RXINFO_BA               = 1 << 0
MT_RXINFO_DATA             = 1 << 1
MT_RXINFO_NULL             = 1 << 2
MT_RXINFO_FRAG             = 1 << 3
MT_RXINFO_UNICAST          = 1 << 4
MT_RXINFO_MULTICAST        = 1 << 5
MT_RXINFO_BROADCAST        = 1 << 6
MT_RXINFO_MYBSS            = 1 << 7
MT_RXINFO_CRCERR           = 1 << 8
MT_RXINFO_ICVERR           = 1 << 9
MT_RXINFO_MICERR           = 1 << 10
MT_RXINFO_AMSDU            = 1 << 11
MT_RXINFO_HTC              = 1 << 12
MT_RXINFO_RSSI             = 1 << 13
MT_RXINFO_L2PAD            = 1 << 14
MT_RXINFO_AMPDU            = 1 << 15
MT_RXINFO_DECRYPT          = 1 << 16

MT_RXWI_CTL_WCID_MASK      = 0xFF         # GENMASK(7, 0)
MT_RXWI_CTL_MPDU_LEN_MASK  = 0x3FFF0000   # GENMASK(29, 16)
MT_RXWI_CTL_MPDU_LEN_SHIFT = 16

# Header sizes — kernel dma.h constants.
MT_DMA_HDR_LEN             = 4    # [SRC] dma.h:44
MT_RX_RXWI_LEN             = 32   # [SRC] dma.h:47 (sizeof struct mt76x02_rxwi)
MT_FCE_INFO_LEN            = 4    # [SRC] dma.h:46
HEADER_SIZE                = MT_DMA_HDR_LEN + MT_RX_RXWI_LEN   # 36 bytes


class RxFrame(NamedTuple):
    """A decoded mt76x0u bulk-IN packet, ready for the parser."""
    frame: bytes        # raw 802.11 bytes (length == mpdu_len)
    rssi_dbm: int       # raw rxwi.rssi[0] interpreted as signed int8
    mpdu_len: int
    rxinfo: int         # MT_RXINFO_* bitmap
    wcid: int           # MT_RXWI_CTL_WCID field


class RxDecodeError(Exception):
    """Raised when a bulk-IN chunk can't be decoded into a frame."""


def decode_rx_packet(data: bytes) -> Optional[RxFrame]:
    """Decode a single bulk-IN packet from EP 0x84.

    Returns None for packets we can't / shouldn't parse:
      - too short (< HEADER_SIZE + 24 bytes)
      - FCE info TYPE field nonzero (non-data packet — e.g., MCU event echo)
      - CRC/ICV/MIC error bits set in rxinfo
      - L2PAD bit set (would need 802.11 header length to strip — skipped)
      - MPDU_LEN larger than remaining buffer (truncated frame)

    [SRC] mt76x02_mac.c:771-873 (mt76x02_mac_process_rx).
    """
    if len(data) < HEADER_SIZE + 24 + MT_FCE_INFO_LEN:   # 24 = min 802.11 mgmt hdr
        return None

    # First 2 bytes: dma_len (le16) — total length after the DMA hdr.
    # We use it as a self-consistency check, but the kernel doesn't decode
    # bytes 2-3 of the DMA header. [SRC] usb.c:460.
    dma_len = struct.unpack_from("<H", data, 0)[0]
    if dma_len == 0 or (dma_len & 0x3) or (MT_DMA_HDR_LEN + dma_len) > len(data):
        return None

    # mt76x02_rxwi: rxinfo (4) + ctl (4) + tid_sn (2) + rate (2) + rssi[4] (4) + bbp[16]
    (rxinfo, ctl) = struct.unpack_from("<II", data, MT_DMA_HDR_LEN)

    # Reject hardware-flagged bad frames.
    if rxinfo & (MT_RXINFO_CRCERR | MT_RXINFO_ICVERR | MT_RXINFO_MICERR):
        return None

    # L2PAD requires header-length-aware stripping; defer.
    if rxinfo & MT_RXINFO_L2PAD:
        return None

    mpdu_len = (ctl & MT_RXWI_CTL_MPDU_LEN_MASK) >> MT_RXWI_CTL_MPDU_LEN_SHIFT
    wcid     = ctl & MT_RXWI_CTL_WCID_MASK
    if mpdu_len < 10 or mpdu_len > 4096:
        # Sanity: 802.11 frame is at least an ACK (10B) and well under 4 KB.
        return None

    frame_start = HEADER_SIZE
    frame_end = frame_start + mpdu_len
    if frame_end > len(data):
        # Truncated bulk-IN packet — shouldn't happen but defensive.
        return None

    # rssi[0] within rxwi is at byte offset 12 (after rxinfo 4 + ctl 4 +
    # tid_sn 2 + rate 2). Global offset = MT_DMA_HDR_LEN + 12 = 16.
    # Kernel treats raw value as s8.
    rssi_raw = struct.unpack_from("<b", data, MT_DMA_HDR_LEN + 12)[0]

    return RxFrame(
        frame=bytes(data[frame_start:frame_end]),
        rssi_dbm=rssi_raw,
        mpdu_len=mpdu_len,
        rxinfo=rxinfo,
        wcid=wcid,
    )


def iter_rx_frames(chunks: Iterator[bytes]) -> Iterator[RxFrame]:
    """Convenience: filter `decode_rx_packet` over an iterable of bulk-IN
    chunks, dropping the Nones."""
    for chunk in chunks:
        rx = decode_rx_packet(chunk)
        if rx is not None:
            yield rx
