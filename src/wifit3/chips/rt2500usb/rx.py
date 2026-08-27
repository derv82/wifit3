"""rt2500usb RX path: bulk-IN URB → 802.11 MPDU + RSSI.

URB layout (rt2500usb.c:1216-1287, rt2500usb_fill_rxdone). Unlike most
chips, the RX descriptor **trails** the frame data:

    [802.11 frame (DATABYTE_COUNT bytes)] [align pad] [RXD (16B)]

The kernel locates the RXD at ``skb->data + (actual_length - desc_size)``
and trims the skb to ``RXD_W0_DATABYTE_COUNT``. So:
  * RXD       = buf[-16:]            (4 x __le32)
  * frame     = buf[0 : DATABYTE_COUNT]
  * RSSI dBm  = RXD_W1_RSSI - rssi_offset   (offset 120 unless EEPROM-set)
  * RXD_W0 carries CRC/PHYSICAL error + OFDM/MY_BSS flags

Verified against driver_captures/captures_rt2500usb/capture-2 frame 1453: a
232-byte URB decoded to a 215-byte CCK beacon at RSSI -46 dBm with one
alignment-pad byte before the descriptor.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Optional

import usb.core

from .constants import (
    DEFAULT_RSSI_OFFSET,
    RXD_DESC_SIZE,
    RXD_W0_CRC_ERROR,
    RXD_W0_DATABYTE_COUNT,
    RXD_W0_MY_BSS,
    RXD_W0_OFDM,
    RXD_W0_PHYSICAL_ERROR,
    RXD_W1_RSSI,
    RXD_W1_SIGNAL,
)

logger = logging.getLogger(__name__)


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
    """Discover bulk endpoints. RT2570 exposes bulk EP 0x81 IN (RX) and
    EP 0x01 OUT (TX)."""
    cfg = dev.get_active_configuration()
    intf = cfg[(interface, 0)]
    bulk_in: list[int] = []
    bulk_out: list[int] = []
    for ep in intf:
        addr = ep.bEndpointAddress
        if (ep.bmAttributes & 0x03) != 0x02:   # bulk only
            continue
        (bulk_in if addr & 0x80 else bulk_out).append(addr)
    logger.debug(
        "endpoints: bulk_in=%s bulk_out=%s",
        [f"0x{e:02x}" for e in bulk_in],
        [f"0x{e:02x}" for e in bulk_out],
    )
    return Endpoints(bulk_in=bulk_in, bulk_out=bulk_out)


@dataclass(frozen=True)
class RxFrame:
    mpdu: bytes
    rssi_dbm: int
    signal: int
    ofdm: bool
    my_bss: bool
    has_fcs_error: bool


def parse_rx_urb(buf: bytes, *, rssi_offset: int = DEFAULT_RSSI_OFFSET) -> Optional[RxFrame]:
    """Decode one bulk-IN URB → RxFrame, or None if malformed.

    Returns None when the URB is too short to hold a descriptor, or the
    descriptor's frame length doesn't fit in the URB.
    """
    if len(buf) < RXD_DESC_SIZE:
        return None

    rxd = buf[-RXD_DESC_SIZE:]
    word0, word1 = struct.unpack_from("<II", rxd, 0)

    size = (word0 & RXD_W0_DATABYTE_COUNT) >> 16
    if size < 2 or size > len(buf) - RXD_DESC_SIZE:
        # Frame must fit before the trailing descriptor (allowing for the
        # optional 1-byte alignment pad).
        return None

    rssi_raw = word1 & RXD_W1_RSSI
    signal = (word1 & RXD_W1_SIGNAL) >> 8

    # rt2500usb hardware reports DATABYTE_COUNT inclusive of the on-air
    # 4-byte 802.11 FCS — strip it so consumers see only the MPDU body.
    # Confirmed via FCSDIAG CRC tally on hardware (100% has_fcs ratio); the
    # has_fcs_error flag above is set separately by the chip and survives
    # this strip.
    return RxFrame(
        mpdu=bytes(buf[:max(size - 4, 0)]),
        rssi_dbm=rssi_raw - rssi_offset,
        signal=signal,
        ofdm=bool(word0 & RXD_W0_OFDM),
        my_bss=bool(word0 & RXD_W0_MY_BSS),
        has_fcs_error=bool(word0 & (RXD_W0_CRC_ERROR | RXD_W0_PHYSICAL_ERROR)),
    )


def read_rx_burst(
    dev: usb.core.Device,
    ep: int,
    *,
    max_size: int = 2432 + RXD_DESC_SIZE + 64,
    timeout_ms: int = 100,
) -> Optional[bytes]:
    """One bulk-IN read. Returns None on timeout, bytes on success.

    ``max_size`` covers a full DATA_FRAME_SIZE (2432) frame + the 16-byte
    RXD + slack for alignment / a trailing short packet.
    """
    try:
        data = dev.read(ep, max_size, timeout_ms)
        return bytes(data)
    except usb.core.USBError as e:
        err = getattr(e, "errno", None)
        if err in (110, 10060) or "timeout" in str(e).lower():
            return None
        raise
