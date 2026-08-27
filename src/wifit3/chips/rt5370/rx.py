"""RX path for the RT5370 (RT5390): bulk-IN URB → 802.11 MPDU + RSSI.

Ported from ``rt2800usb_fill_rxdone`` (rt2800usb.c:481-577) + ``rt2800_process_rxwi`` /
``rt2800_agc_to_rssi`` (rt2800lib.c:856-942). The bulk-IN stream is chip→host and never
enters the control gate, so this is verified on hardware (beacon-count A/B), not by
``verify_pcap``; the descriptor offsets are re-derived from the kernel + rt2800usb.h.

URB layout (one or more aggregated frames):

    | RXINFO(4) | RXWI(16) | header | L2 pad | payload | pad | RXD(4) | USB pad |
               |<------------------ rx_pkt_len ------------------>|

  * RXINFO_W0[15:0]  = rx_pkt_len  (RXWI + frame + L2 pad + frame pad)
  * RXWI_W0[27:16]   = MPDU_TOTAL_BYTE_COUNT — the 802.11 frame length (FCS already
                       excluded by the chip, so frame_end == MPDU_end
                       [[project_rx_frames_include_fcs]])
  * RXWI_W2          = signed per-path RSSI bytes (this 1T1R card uses path 0)
  * RXD (at RXINFO_DESC_SIZE + rx_pkt_len): CRC_ERROR / L2PAD / MY_BSS

RT539x uses a 16-byte (4-word) RXWI — same width as the RF30xx family (RT5592 is the
6-word outlier this driver does not claim).

L2 pad: the chip inserts 2 alignment bytes between the MAC header and the body when the
header length isn't 4-aligned (every QoS-Data frame — the EAPOL carrier). RXD_W0_L2PAD
flags it; remove it before trimming to MPDU_TOTAL_BYTE_COUNT.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Iterator

import usb.core

from ..log_trace import TRACE
from . import constants as C
from .constants import get_field
from .eeprom import EepromValues

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Endpoints:
    bulk_in: list[int]
    bulk_out: list[int]

    @property
    def primary_bulk_in(self) -> int:
        if not self.bulk_in:
            raise RuntimeError("rt5370: no bulk-IN endpoint found")
        return self.bulk_in[0]


def probe_endpoints(dev: usb.core.Device, *, interface: int = 0) -> Endpoints:
    """Enumerate the card's bulk endpoints [SRC rt2x00usb.c rt2x00usb_find_endpoints]."""
    cfg = dev.get_active_configuration()
    intf = cfg[(interface, 0)]
    bulk_in: list[int] = []
    bulk_out: list[int] = []
    for ep in intf:
        if (ep.bmAttributes & 0x03) != 0x02:        # 0x02 = bulk
            continue
        (bulk_in if ep.bEndpointAddress & 0x80 else bulk_out).append(ep.bEndpointAddress)
    logger.debug("rt5370 endpoints: bulk_in=%s bulk_out=%s",
                [f"0x{e:02x}" for e in bulk_in], [f"0x{e:02x}" for e in bulk_out])
    return Endpoints(bulk_in=bulk_in, bulk_out=bulk_out)


def _ieee80211_hdrlen(fc0: int, fc1: int) -> int:
    """802.11 header length from the first two frame-control bytes (mirrors
    ieee80211_hdrlen): 24 base, +6 for 4-address WDS, +2 for QoS-Data; control
    frames are 10."""
    ftype = (fc0 & 0x0C) >> 2
    subtype = (fc0 & 0xF0) >> 4
    if ftype == 1:                                   # control
        return 10
    base = 30 if (fc1 & 0x03) == 0x03 else 24        # to_ds & from_ds ⇒ WDS
    if ftype == 2 and (subtype & 0x08):              # QoS-Data
        base += 2
    return base


def agc_to_rssi(rxwi_w2: int, ev: EepromValues, lna_gain: int) -> int:
    """RXWI signed RSSI bytes → dBm [SRC rt2800lib.c:856-898 rt2800_agc_to_rssi].
    2.4 GHz: ``base(-12) - eeprom_offset - lna_gain - raw``; a 0 raw byte ⇒ -128
    (invalid). mac80211 takes a single value, so the strongest path wins."""
    def s8(b: int) -> int:
        return b - 0x100 if b >= 0x80 else b

    raw = (get_field(rxwi_w2, C.RXWI_W2_RSSI0),
           get_field(rxwi_w2, C.RXWI_W2_RSSI1),
           get_field(rxwi_w2, C.RXWI_W2_RSSI2))
    rssi = [C.RSSI_BASE_VAL - off - lna_gain - s8(r) if r else -128
            for r, off in zip(raw, ev.rssi_offset_bg)]
    return max(rssi)


def iter_frames(buf: bytes, ev: EepromValues, lna_gain: int) -> Iterator[tuple[bytes, int]]:
    """Split one bulk-IN buffer into ``(mpdu, rssi_dbm)`` for each aggregated frame.

    Skips CRC-error frames (the chip can still deliver them) and anything malformed.
    """
    off = 0
    n = len(buf)
    while off + C.RXINFO_DESC_SIZE + C.RXWI_DESC_SIZE_4WORDS + C.RXD_DESC_SIZE <= n:
        rxinfo_w0 = struct.unpack_from("<I", buf, off)[0]
        rx_pkt_len = get_field(rxinfo_w0, C.RXINFO_W0_USB_DMA_RX_PKT_LEN)
        if rx_pkt_len == 0 or off + C.RXINFO_DESC_SIZE + rx_pkt_len + C.RXD_DESC_SIZE > n:
            break

        rxwi_off = off + C.RXINFO_DESC_SIZE
        rxwi_w0 = struct.unpack_from("<I", buf, rxwi_off)[0]
        rxwi_w2 = struct.unpack_from("<I", buf, rxwi_off + 8)[0]
        mpdu_len = get_field(rxwi_w0, C.RXWI_W0_MPDU_TOTAL_BYTE_COUNT)

        rxd_w0 = struct.unpack_from("<I", buf, off + C.RXINFO_DESC_SIZE + rx_pkt_len)[0]
        crc_error = bool(rxd_w0 & C.RXD_W0_CRC_ERROR)
        l2pad = bool(rxd_w0 & C.RXD_W0_L2PAD)

        frame_start = rxwi_off + C.RXWI_DESC_SIZE_4WORDS
        body = buf[frame_start:off + C.RXINFO_DESC_SIZE + rx_pkt_len]
        if l2pad and len(body) >= 2:
            hdrlen = _ieee80211_hdrlen(body[0], body[1])
            if 0 < hdrlen <= len(body) - 2:
                body = body[:hdrlen] + body[hdrlen + 2:]

        if 4 <= mpdu_len <= len(body) and not crc_error:
            yield bytes(body[:mpdu_len]), agc_to_rssi(rxwi_w2, ev, lna_gain)

        # Advance to the next aggregated frame (RXINFO + payload + RXD, 4-byte aligned).
        stride = (C.RXINFO_DESC_SIZE + rx_pkt_len + C.RXD_DESC_SIZE + 3) & ~3
        off += stride


def read_rx_burst(dev: usb.core.Device, ep: int, *, max_size: int = 16384,
                  timeout_ms: int = 100) -> bytes | None:
    """One bulk-IN read; ``None`` on timeout. At TRACE, logs each read so the exact
    moment RX goes silent (data → timeouts) is visible in the wedge preamble."""
    try:
        data = bytes(dev.read(ep, max_size, timeout_ms))
        if logger.isEnabledFor(TRACE):
            logger.trace("RX bulk-IN <%dB>", len(data))
        return data
    except usb.core.USBError as e:
        err = getattr(e, "errno", None)
        if err in (110, 10060) or "timeout" in str(e).lower():
            if logger.isEnabledFor(TRACE):
                logger.trace("RX bulk-IN timeout")
            return None
        raise
