"""RTL8187 RX path: bulk-IN URB → 802.11 MPDU + RSSI.

Ported from ``rtl8187_rx_cb`` (dev.c:325-412). The 8187B trailer is the 20-byte
``rtl8187b_rx_hdr`` (rtl8187.h:53-63), on bulk-IN 0x83.

URB layout (8187L):

    [802.11 frame (FCS-inclusive) | trailing rtl8187_rx_hdr (16 bytes)]

The 16-byte trailer struct (rtl8187.h:44-51):

    __le32 flags;        ← bits 0..11 = frame length (FCS-inclusive)
                          bits 13    = CRC32 error
                          bits 20..23 = rate index
                          bits 25    = short preamble
    u8     noise;
    u8     signal;        ← bit 7 = antenna A/B selector
    u8     agc;
    u8     reserved;
    __le64 mac_time;

Each bulk-IN URB carries exactly one frame on 8187L (no batching). The
chip is configured with RX_INCLUDES_FCS, so the frame length in the
trailer includes the 4-byte FCS — we strip it before handing the
payload to the WlanFrameParser.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Optional

import usb.core

from .constants import (
    RTL8187_MAX_RX,
    RX_DESC_FLAG_CRC32_ERR,
    RX_HDR_SIZE_8187B,
    RX_HDR_SIZE_8187L,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Endpoint discovery — mirror of rtw_usb_parse from the 8821au driver.
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Endpoints:
    bulk_in: list[int]
    bulk_out: list[int]

    @property
    def primary_bulk_in(self) -> int:
        if not self.bulk_in:
            raise RuntimeError("no bulk-IN endpoint found")
        return self.bulk_in[0]


def probe_endpoints(dev: usb.core.Device, *, interface: int = 0) -> Endpoints:
    """Enumerate the active configuration's endpoints and classify them.

    The 8187L exposes a single configuration with bulk-IN 0x81 + bulk-OUT
    0x02 (per the AWUS036H ground truth) but doing the probe properly
    lets us match the rest of the driver family.
    """
    cfg = dev.get_active_configuration()
    intf = cfg[(interface, 0)]
    bulk_in: list[int] = []
    bulk_out: list[int] = []
    for ep in intf:
        addr = ep.bEndpointAddress
        attr = ep.bmAttributes & 0x03
        if attr != 0x02:  # not bulk
            continue
        if addr & 0x80:
            bulk_in.append(addr)
        else:
            bulk_out.append(addr)
    logger.debug(
        "endpoints: bulk_in=%s bulk_out=%s",
        [f"0x{e:02x}" for e in bulk_in],
        [f"0x{e:02x}" for e in bulk_out],
    )
    return Endpoints(bulk_in=bulk_in, bulk_out=bulk_out)


# ----------------------------------------------------------------------
# Decoded RX frame
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class RxFrame:
    """One bulk-IN URB after the trailing rx_hdr has been peeled off.

    ``mpdu`` is the 802.11 frame with the 4-byte FCS already removed —
    ready for ``WlanFrameParser.parse_80211_frame``.
    """
    mpdu: bytes
    rssi_dbm: int
    mac_time: int
    flags: int
    rate_idx: int
    antenna: int       # 0 or 1
    has_fcs_error: bool


def parse_rx_urb(buf: bytes, is_rtl8187b: bool) -> Optional[RxFrame]:
    """Decode one bulk-IN URB → RxFrame, or None if malformed (rtl8187_rx_cb, dev.c:346-381).

    Returns None for:
      * URBs too short to even contain a trailer
      * trailer-reported frame length that doesn't fit in the URB
      * frames shorter than (FCS=4) — can't be a valid 802.11 MPDU
    """
    hdr_size = RX_HDR_SIZE_8187B if is_rtl8187b else RX_HDR_SIZE_8187L
    if len(buf) < hdr_size:
        return None

    hdr_off = len(buf) - hdr_size
    if not is_rtl8187b:
        flags, _noise, signal, agc, _reserved, mac_time = struct.unpack(
            "<IBBBBQ", buf[hdr_off:])
        # dev.c:354 — scaling constants derived from p54usb
        rssi_dbm = -4 - ((27 * agc) >> 6)
        antenna = (signal >> 7) & 1
    else:
        # rtl8187b_rx_hdr: flags, mac_time, sq, rssi, agc, flags2, snr_long2end, pwdb_g12, fot
        flags, mac_time, _sq, rssi, agc, _flags2, _snr, _pwdb_g12, _fot = struct.unpack(
            "<IQBBBBHbB", buf[hdr_off:])
        rssi_dbm = 14 - agc // 2                  # dev.c:373
        antenna = (rssi >> 7) & 1

    frame_len = flags & 0x0FFF
    rate_idx = (flags >> 20) & 0xF
    has_fcs_error = bool(flags & RX_DESC_FLAG_CRC32_ERR)

    if frame_len < 4 or frame_len > hdr_off:
        # Trailer claims a length we can't satisfy from the URB.
        return None

    # FCS-inclusive length per RX_INCLUDES_FCS; strip the final 4 bytes
    # before handing the payload to the parser.
    mpdu = bytes(buf[: frame_len - 4])

    return RxFrame(
        mpdu=mpdu,
        rssi_dbm=rssi_dbm,
        mac_time=mac_time,
        flags=flags,
        rate_idx=rate_idx,
        antenna=antenna,
        has_fcs_error=has_fcs_error,
    )


def read_rx_burst(
    dev: usb.core.Device,
    ep: int,
    *,
    max_size: int = RTL8187_MAX_RX,
    timeout_ms: int = 100,
) -> Optional[bytes]:
    """Single bulk-IN read. Returns None on timeout, bytes on success.

    PyUSB raises ``usb.core.USBError`` (errno=110 on Linux, 10060 on
    Windows) for timeout — we translate to None so callers can poll
    in a tight loop without ``try/except``.
    """
    try:
        data = dev.read(ep, max_size, timeout_ms)
        return bytes(data)
    except usb.core.USBError as e:
        err = getattr(e, "errno", None)
        if err in (110, 10060) or "timeout" in str(e).lower():
            return None
        raise
