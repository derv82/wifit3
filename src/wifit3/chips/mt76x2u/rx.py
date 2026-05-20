"""MT76x2U RX bulk-IN drain + frame decode.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

Mirrors mt76x02_mac.c::mt76x02_mac_process_rx.

Wire format on bulk-IN EP 0x84 (one frame per URB):

    [4B  rxfce        ]   bits 13:0  = LEN (incl. RXWI)
                          bits 31:30 = TYPE (=1 for RX)
    [32B mt76x02_rxwi ]   rxinfo:u32, ctl:u32, tid_sn:u16, rate:u16,
                          rssi[4]:u8, bbp_rxinfo[4]:u32
    [N   802.11 frame ]   length from ctl.MPDU_LEN
    [pad / FCS        ]

(`[SRC] mt76x02_mac.h:97` for rxwi, `mt76x02_dma.h:23` for rxfce, and
`mt76x02_mac.c:771` for the kernel decoder.)
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Optional

import usb.core

from .constants import EP_IN_PKT_RX
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)

# RXWI bitfield positions ([SRC] mt76x02_mac.h:46).
RXINFO_BA           = 1 << 0
RXINFO_NULL         = 1 << 2
RXINFO_UNICAST      = 1 << 4
RXINFO_BROADCAST    = 1 << 6
RXINFO_MYBSS        = 1 << 7
RXINFO_CRCERR       = 1 << 8
RXINFO_AMSDU        = 1 << 11
RXINFO_RSSI         = 1 << 13
RXINFO_L2PAD        = 1 << 14
RXINFO_AMPDU        = 1 << 15
RXINFO_DECRYPT      = 1 << 16
RXINFO_BEACON       = 1 << 25
RXINFO_PROBE_RESP   = 1 << 24

# ctl.MPDU_LEN occupies bits 29:16.
_CTL_MPDU_LEN_SHIFT = 16
_CTL_MPDU_LEN_MASK  = 0x3FFF

# Wire layout offsets.
_RXFCE_LEN          = 4
_RXWI_LEN           = 32
_HEADER_LEN         = _RXFCE_LEN + _RXWI_LEN   # 36 bytes prefix before 802.11


def decode_urb(urb: bytes) -> Optional[dict]:
    """Parse one bulk-IN URB into a decoded frame record.

    Returns a dict with `frame_bytes`, `rssi`, `rxinfo`, and a few flags,
    or None if the URB is too short / has CRC error / etc.
    """
    if len(urb) < _HEADER_LEN:
        return None

    # rxfce header — currently only used for total-len sanity.
    rxfce = struct.unpack("<I", urb[:_RXFCE_LEN])[0]
    rxfce_len = rxfce & 0x3FFF   # MT_RX_FCE_INFO_LEN

    # RXWI fields.
    rxinfo = struct.unpack("<I", urb[4:8])[0]
    ctl = struct.unpack("<I", urb[8:12])[0]
    # tid_sn = urb[12:14], rate = urb[14:16]  — not used in M4 yet
    rssi_chain = urb[16:20]      # rssi[4]
    # bbp_rxinfo = urb[20:36] — 16 bytes, ignored

    if rxinfo & RXINFO_CRCERR:
        return None

    mpdu_len = (ctl >> _CTL_MPDU_LEN_SHIFT) & _CTL_MPDU_LEN_MASK
    if mpdu_len < 10:   # smaller than any 802.11 header → bogus
        return None
    if _HEADER_LEN + mpdu_len > len(urb):
        # Truncated URB; trim to what's there.
        mpdu_len = len(urb) - _HEADER_LEN

    frame = urb[_HEADER_LEN:_HEADER_LEN + mpdu_len]

    # mt76x02 inserts a 2-byte L2 alignment pad between 802.11 header
    # and body when the MAC header isn't 4-byte aligned. Kernel does
    # `mt76x02_remove_hdr_pad` to slide the header forward; for our
    # parser the easier route is to drop those 2 bytes from offset
    # hdrlen onward.
    if rxinfo & RXINFO_L2PAD:
        # 802.11 frame_control bits give us hdrlen.
        if len(frame) >= 2:
            hdrlen = _ieee80211_hdrlen(frame[0], frame[1])
            if 0 < hdrlen < len(frame) - 2:
                frame = frame[:hdrlen] + frame[hdrlen + 2:]

    # FCS strip — mt76 leaves the trailing 4-byte FCS in mpdu_len. The
    # WlanFrameParser doesn't validate FCS; we strip it so payload offsets
    # match without surprise.
    if len(frame) >= 4:
        frame = frame[:-4]

    # RSSI: prefer chain 0. The kernel applies a gain offset
    # (mt76x02_mac_get_rssi); for M4 we use the raw byte (signed int8).
    rssi = struct.unpack("<b", bytes([rssi_chain[0]]))[0]

    return {
        "frame_bytes": frame,
        "rssi": rssi,
        "rxinfo": rxinfo,
        "rxfce_len": rxfce_len,
        "is_beacon": bool(rxinfo & RXINFO_BEACON),
        "is_probe_resp": bool(rxinfo & RXINFO_PROBE_RESP),
        "is_broadcast": bool(rxinfo & RXINFO_BROADCAST),
    }


def _ieee80211_hdrlen(fc0: int, fc1: int) -> int:
    """Compute the 802.11 header length from frame_control bytes."""
    ftype = (fc0 & 0x0C) >> 2
    subtype = (fc0 & 0xF0) >> 4
    if ftype == 1:  # CTRL
        # Most ctrl frames are 16 bytes (RTS/CTS/ACK).
        return 10
    # MGMT / DATA: 24 bytes for non-WDS, 30 for WDS (to_ds & from_ds).
    base = 24
    if (fc1 & 0x03) == 0x03:
        base = 30
    # QoS data adds 2 bytes of QoS control.
    if ftype == 2 and (subtype & 0x08):
        base += 2
    return base


class RxDrainer:
    """Background reader on EP 0x84.

    For each URB: parse → decode → dispatch to the registered callback.
    The callback receives the decoded dict shaped like other wifit3 drivers:
    keys produced by `WlanFrameParser.parse_80211_frame`.
    """

    def __init__(self, transport: MT76x2UTransport,
                 frame_callback: Optional[callable] = None,
                 raw_callback: Optional[callable] = None,
                 max_urb_bytes: int = 4096):
        self.transport = transport
        self.frame_callback = frame_callback
        self.raw_callback = raw_callback
        self.max_urb_bytes = max_urb_bytes
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.rx_count = 0
        self.frames_decoded = 0
        self.frames_dropped = 0
        self.first_frame: Optional[bytes] = None
        self.beacon_count = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                data = await self.transport.async_read_bulk(
                    EP_IN_PKT_RX, self.max_urb_bytes, timeout_ms=100,
                )
            except usb.core.USBTimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("RX drain error: %s", e)
                await asyncio.sleep(0.01)
                continue
            if not data:
                continue
            buf = bytes(data)
            self.rx_count += 1
            if self.first_frame is None:
                self.first_frame = buf
            if self.raw_callback is not None:
                try:
                    self.raw_callback(buf)
                except Exception as e:
                    logger.debug("RX raw_callback error: %s", e)

            decoded = decode_urb(buf)
            if decoded is None:
                self.frames_dropped += 1
                continue
            self.frames_decoded += 1
            if decoded["is_beacon"]:
                self.beacon_count += 1
            if self.frame_callback is not None:
                try:
                    self.frame_callback(decoded)
                except Exception as e:
                    logger.debug("RX frame_callback error: %s", e)
