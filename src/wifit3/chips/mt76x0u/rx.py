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
                  body — removed before trimming to MPDU_LEN, mirroring
                  mt76x02_remove_hdr_pad → pskb_trim.)
  ...     4     FCE info trailer (le32) — at the end, NOT the beginning
  END     ...   alignment padding to 4-byte boundary

Total header size before the 802.11 frame = MT_DMA_HDR_LEN + MT_RX_RXWI_LEN
= 4 + 32 = 36 bytes.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Callable, Iterator, NamedTuple, Optional

import usb.core

from .constants import EP_IN_PKT_RX
from ..rx_reader import RxReaderThread

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
      - MPDU_LEN larger than the (de-padded) remaining buffer (truncated frame)

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

    mpdu_len = (ctl & MT_RXWI_CTL_MPDU_LEN_MASK) >> MT_RXWI_CTL_MPDU_LEN_SHIFT
    wcid     = ctl & MT_RXWI_CTL_WCID_MASK
    if mpdu_len < 10 or mpdu_len > 4096:
        # Sanity: 802.11 frame is at least an ACK (10B) and well under 4 KB.
        return None

    frame_start = HEADER_SIZE

    # rssi[0] within rxwi is at byte offset 12 (after rxinfo 4 + ctl 4 +
    # tid_sn 2 + rate 2). Global offset = MT_DMA_HDR_LEN + 12 = 16.
    # Kernel treats raw value as s8.
    rssi_raw = struct.unpack_from("<b", data, MT_DMA_HDR_LEN + 12)[0]

    # Everything after the RXWI prefix: [802.11 hdr][L2 pad?][body][FCS?].
    body = bytes(data[frame_start:])

    # L2 alignment pad: mt76x02 inserts 2 bytes between the 802.11 header and the
    # body when the header isn't 4-byte aligned — every QoS-Data frame (26-byte
    # header), which is what EAPOL rides on. Remove the pad BEFORE trimming to
    # MPDU_LEN: MPDU_LEN counts the de-padded MPDU, so trimming first would drop
    # the last 2 body bytes (for EAPOL, the tail of key_data → no M2 hashline).
    # [SRC] mt76x02_mac.c:831,854 — remove_hdr_pad precedes pskb_trim.
    if rxinfo & MT_RXINFO_L2PAD and len(body) >= 2:
        hdrlen = _ieee80211_hdrlen(body[0], body[1])
        if 0 < hdrlen <= len(body) - 2:
            body = body[:hdrlen] + body[hdrlen + 2:]

    if mpdu_len > len(body):
        # Truncated bulk-IN packet — shouldn't happen but defensive.
        return None

    # Trim to MPDU_LEN; excludes the trailing FCS (and any AMPDU tail padding).
    frame = body[:mpdu_len]

    return RxFrame(
        frame=frame,
        rssi_dbm=rssi_raw,
        mpdu_len=len(frame),    # post-pad-strip + FCS-strip
        rxinfo=rxinfo,
        wcid=wcid,
    )


def _ieee80211_hdrlen(fc0: int, fc1: int) -> int:
    """Compute the 802.11 MAC header length from frame_control bytes.
    [SRC] mt76x2u/rx.py:124-138 (same logic — IEEE 802.11-2020 §9.2)."""
    ftype = (fc0 & 0x0C) >> 2
    subtype = (fc0 & 0xF0) >> 4
    if ftype == 1:    # CTRL — RTS/CTS/ACK most common, 10 bytes
        return 10
    base = 24
    if (fc1 & 0x03) == 0x03:    # WDS: ToDS|FromDS → 4 addresses
        base = 30
    if ftype == 2 and (subtype & 0x08):    # QoS DATA → +2 QoS control bytes
        base += 2
    return base


def decode_rx_inventory(data: bytes) -> Optional[dict]:
    """Diagnostic decoder — returns a structured dict with EVERY field of the
    RX descriptor + the first 32 bytes of the 802.11 frame, without filtering
    or stripping ANYTHING. Used by --phase rx_inventory to figure out what
    the chip is actually delivering and what we're (mis)dropping.

    Returns None only for buffers too small to contain a header. Specifically
    does NOT drop on CRCERR / L2PAD / MPDU_LEN out of range — those are
    reported as fields so the caller can decide.
    """
    if len(data) < MT_DMA_HDR_LEN + MT_RX_RXWI_LEN:
        return {"too_short": True, "raw_len": len(data)}

    dma_len = struct.unpack_from("<H", data, 0)[0]
    rxinfo, ctl = struct.unpack_from("<II", data, MT_DMA_HDR_LEN)
    tid_sn = struct.unpack_from("<H", data, MT_DMA_HDR_LEN + 8)[0]
    rate   = struct.unpack_from("<H", data, MT_DMA_HDR_LEN + 10)[0]
    rssi   = list(struct.unpack_from("<bbbb", data, MT_DMA_HDR_LEN + 12))

    mpdu_len = (ctl & MT_RXWI_CTL_MPDU_LEN_MASK) >> MT_RXWI_CTL_MPDU_LEN_SHIFT
    wcid     = ctl & MT_RXWI_CTL_WCID_MASK
    frame_start = HEADER_SIZE
    frame_end = min(frame_start + mpdu_len, len(data))
    raw_frame = bytes(data[frame_start:frame_end])
    fc0 = raw_frame[0] if raw_frame else 0
    fc1 = raw_frame[1] if len(raw_frame) > 1 else 0
    ftype = (fc0 & 0x0C) >> 2
    subtype = (fc0 & 0xF0) >> 4

    flags = []
    for name, mask in [
        ("BA", MT_RXINFO_BA), ("DATA", MT_RXINFO_DATA),
        ("NULL", MT_RXINFO_NULL), ("FRAG", MT_RXINFO_FRAG),
        ("UNICAST", MT_RXINFO_UNICAST), ("MCAST", MT_RXINFO_MULTICAST),
        ("BCAST", MT_RXINFO_BROADCAST), ("MYBSS", MT_RXINFO_MYBSS),
        ("CRCERR", MT_RXINFO_CRCERR), ("ICVERR", MT_RXINFO_ICVERR),
        ("MICERR", MT_RXINFO_MICERR), ("AMSDU", MT_RXINFO_AMSDU),
        ("HTC", MT_RXINFO_HTC), ("RSSI", MT_RXINFO_RSSI),
        ("L2PAD", MT_RXINFO_L2PAD), ("AMPDU", MT_RXINFO_AMPDU),
        ("DECRYPT", MT_RXINFO_DECRYPT),
    ]:
        if rxinfo & mask:
            flags.append(name)

    return {
        "too_short": False,
        "raw_len":   len(data),
        "dma_len":   dma_len,
        "rxinfo":    rxinfo,
        "ctl":       ctl,
        "tid_sn":    tid_sn,
        "rate":      rate,
        "rssi":      rssi,
        "wcid":      wcid,
        "mpdu_len":  mpdu_len,
        "flags":     flags,
        "fc0":       fc0,
        "fc1":       fc1,
        "ftype":     ftype,     # 0=MGMT, 1=CTRL, 2=DATA
        "subtype":   subtype,
        "frame_head_hex": raw_frame[:32].hex(" "),
        "frame_len_seen": len(raw_frame),
    }


def iter_rx_frames(chunks: Iterator[bytes]) -> Iterator[RxFrame]:
    """Convenience: filter `decode_rx_packet` over an iterable of bulk-IN
    chunks, dropping the Nones."""
    for chunk in chunks:
        rx = decode_rx_packet(chunk)
        if rx is not None:
            yield rx


# ---------------------------------------------------------------------------
# Async RX drainer — background asyncio task that pulls bulk-IN chunks off
# EP 0x84, decodes each, and pushes the parsed dict to a callback. Required
# for WlanInterface integration (which expects driver.register_rx_callback).
# Pattern mirrored from mt76x2u sibling driver.
# ---------------------------------------------------------------------------

class RxDrainer:
    """Background reader on EP 0x84. For each URB: decode → parse → callback.

    The callback receives a dict shaped like WlanFrameParser.parse_80211_frame
    (which is what WlanInterface._on_frame_parsed expects).
    """

    def __init__(self, transport, frame_callback: Optional[Callable] = None,
                 max_urb_bytes: int = 2048,
                 on_fatal: Optional[Callable] = None):
        self.transport = transport
        self.frame_callback = frame_callback
        self.max_urb_bytes = max_urb_bytes
        self.on_fatal = on_fatal
        self._reader: Optional[RxReaderThread] = None
        # Stats (helpful for debugging)
        self.rx_count = 0
        self.decoded = 0
        self.decode_failures = 0
        self.parse_failures = 0
        self.dispatched = 0

    async def start(self) -> None:
        if self._reader is not None:
            return
        loop = asyncio.get_running_loop()
        self._reader = RxReaderThread(
            loop, self._read_once, self._dispatch, name="mt76x0u-rx",
            on_fatal=self.on_fatal,
        )
        self._reader.start()

    async def stop(self) -> None:
        if self._reader is not None:
            await self._reader.stop()
            self._reader = None

    # read_once runs on the reader thread; dispatch runs on the event loop.

    def _read_once(self) -> Optional[bytes]:
        """One blocking bulk-IN read; None on a benign timeout (bulk_in raises
        usb.core.USBError on timeout, which the reader thread must NOT count)."""
        try:
            return self.transport.bulk_in(
                EP_IN_PKT_RX, self.max_urb_bytes, timeout_ms=100,
            )
        except usb.core.USBError as e:
            if (getattr(e, "backend_error_code", None) == -7
                    or getattr(e, "errno", None) in (110, 10060)
                    or "timeout" in str(e).lower()):
                return None
            raise

    def _dispatch(self, buf: bytes) -> None:
        """Decode one RX packet → parse → frame callback (on the loop)."""
        # Defer the WlanFrameParser import to avoid a circular at module load.
        from wifit3.wlan.packet import WlanFrameParser

        self.rx_count += 1
        rx = decode_rx_packet(bytes(buf))
        if rx is None:
            self.decode_failures += 1
            return
        self.decoded += 1
        parsed = WlanFrameParser.parse_80211_frame(rx.frame, rx.rssi_dbm)
        if parsed is None:
            self.parse_failures += 1
            return
        if self.frame_callback is not None:
            try:
                self.frame_callback(parsed)
                self.dispatched += 1
            except Exception as e:
                logger.debug("RxDrainer callback error: %s", e)
