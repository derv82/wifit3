"""rt2800usb RX path: bulk-IN URB → 802.11 MPDU + RSSI.

URB layout (rt2800usb.c:481-518, rt2800lib.c:900-942):

    [RXINFO (4B)] [RXWI (16B for RT539x, 24B for RT5592)]
    [802.11 frame (MPDU_TOTAL_BYTE_COUNT bytes, possibly L2-padded)]
    [pad] [RXD (4B)] [USB pad]

Where:
  * RXINFO_W0[15:0] = rx_pkt_len  (RXWI + frame + L2 pad + frame pad)
  * RXWI_W0[27:16] = MPDU_TOTAL_BYTE_COUNT  (the actual 802.11 frame length)
  * RXWI_W2[7:0]   = signed RSSI byte for path 0   (per-path 0/1/2)
  * RXD_W0 bit 8   = CRC_ERROR

RSSI formula (rt2800lib.c:856-898):
    rssi = base_val - eeprom_offset - lna_gain - rssi_raw_byte
    base_val = -12 (everything except RT6352)

We defer the EEPROM-driven offset + lna_gain (per
[[feedback_defer_efuse_on_bring_up]]) and use:
    rssi = -12 - max(rssi_raw_byte_path0, ...)
which is a slight over-estimate of signal strength but in the right
ballpark.  EEPROM-aware RSSI lands in M4 alongside the per-channel TX
power tables (both come from the same EEPROM bytes).

L2 padding: the chip inserts 2 padding bytes between the MAC header and
the payload when the header length isn't a multiple of 4 -- every
QoS-Data frame (the carrier for EAPOL; beacons have a 4-aligned 24-byte
header and aren't padded).  RXD_W0_L2PAD flags it, and parse_rx_urb
removes the pad before trimming to MPDU_TOTAL_BYTE_COUNT -- otherwise the
last 2 body bytes (an EAPOL M2's key_data tail) fall outside the window
and the handshake is clipped.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .eeprom import EepromValues

import usb.core

from .constants import (
    RT_RT5592,
    RXD_DESC_SIZE,
    RXD_W0_CRC_ERROR,
    RXD_W0_L2PAD,
    RXINFO_DESC_SIZE,
    RXINFO_W0_USB_DMA_RX_PKT_LEN,
    RXWI_DESC_SIZE_4WORDS,
    RXWI_DESC_SIZE_6WORDS,
    RXWI_W0_MPDU_TOTAL_BYTE_COUNT,
    RXWI_W1_MCS,
    RXWI_W2_RSSI0,
    RXWI_W2_RSSI1,
    RXWI_W2_RSSI2,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Endpoint discovery (same shape as rtl8187/rx.py).
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
    cfg = dev.get_active_configuration()
    intf = cfg[(interface, 0)]
    bulk_in: list[int] = []
    bulk_out: list[int] = []
    for ep in intf:
        addr = ep.bEndpointAddress
        attr = ep.bmAttributes & 0x03
        if attr != 0x02:
            continue
        (bulk_in if addr & 0x80 else bulk_out).append(addr)
    logger.debug(
        "endpoints: bulk_in=%s bulk_out=%s",
        [f"0x{e:02x}" for e in bulk_in],
        [f"0x{e:02x}" for e in bulk_out],
    )
    return Endpoints(bulk_in=bulk_in, bulk_out=bulk_out)


# ----------------------------------------------------------------------
# RX frame
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class RxFrame:
    mpdu: bytes
    rssi_dbm: int
    mcs: int
    has_fcs_error: bool


def rxwi_size_for_silicon(silicon_id: int) -> int:
    """RT5592 uses 6-word RXWI (24 B); everything else in our supported
    set uses 4-word (16 B). [SRC] rt2800lib.c:603-625 rt2800_get_txwi_rxwi_size."""
    if silicon_id == RT_RT5592:
        return RXWI_DESC_SIZE_6WORDS
    return RXWI_DESC_SIZE_4WORDS


# base_val is -12 for every silicon this driver supports (-2 only on RT6352,
# which we don't). [SRC] rt2800lib.c:861.
_RSSI_BASE_VAL = -12


@dataclass(frozen=True)
class RssiCal:
    """The per-band RSSI offsets + per-channel LNA gain that kernel
    rt2800_agc_to_rssi subtracts. All-zero (the default) reproduces the old
    pre-EEPROM ballpark (base_val - raw only)."""
    offset0: int = 0
    offset1: int = 0
    offset2: int = 0
    lna_gain: int = 0


_DEFAULT_RSSI_CAL = RssiCal()


def rssi_cal_for_channel(eeprom: "EepromValues", channel: int) -> RssiCal:
    """Band offsets (rt2800_agc_to_rssi) + channel-range LNA gain
    (rt2800_config_lna_gain) for `channel`. [SRC] rt2800lib.c:867-878, 2407-2437."""
    if channel <= 14:
        return RssiCal(eeprom.rssi_bg_offset0, eeprom.rssi_bg_offset1,
                       eeprom.rssi_bg_offset2, eeprom.lna_gain_bg)
    if channel <= 64:
        lna = eeprom.lna_gain_a          # LNA_A0
    elif channel <= 128:
        lna = eeprom.lna_gain_a1
    else:
        lna = eeprom.lna_gain_a2
    return RssiCal(eeprom.rssi_a_offset0, eeprom.rssi_a_offset1,
                   eeprom.rssi_a_offset2, lna)


def _agc_to_rssi(rxwi_w2: int, cal: RssiCal) -> int:
    """dBm from the strongest RX path — kernel rt2800_agc_to_rssi:
    rssiN = base_val - offsetN - lna_gain - rawN (signed byte), else -128."""
    def _signed8(byte_val: int) -> int:
        return byte_val - 256 if byte_val >= 128 else byte_val

    raw0 = rxwi_w2 & RXWI_W2_RSSI0
    raw1 = (rxwi_w2 & RXWI_W2_RSSI1) >> 8
    raw2 = (rxwi_w2 & RXWI_W2_RSSI2) >> 16

    base = _RSSI_BASE_VAL
    rssi0 = base - cal.offset0 - cal.lna_gain - _signed8(raw0) if raw0 else -128
    rssi1 = base - cal.offset1 - cal.lna_gain - _signed8(raw1) if raw1 else -128
    rssi2 = base - cal.offset2 - cal.lna_gain - _signed8(raw2) if raw2 else -128

    return max(rssi0, rssi1, rssi2)


def _ieee80211_hdrlen(fc0: int, fc1: int) -> int:
    """802.11 header length from the first two frame-control bytes:
    24 base, +6 for WDS (to_ds & from_ds), +2 for QoS-Data; control
    frames are header-only. Mirrors ieee80211_hdrlen."""
    ftype = (fc0 & 0x0C) >> 2
    subtype = (fc0 & 0xF0) >> 4
    if ftype == 1:  # CTRL
        return 10
    base = 24
    if (fc1 & 0x03) == 0x03:  # to_ds & from_ds -> 4-address (WDS)
        base = 30
    if ftype == 2 and (subtype & 0x08):  # QoS-Data
        base += 2
    return base


def parse_rx_urb(
    buf: bytes, *, rxwi_size: int = RXWI_DESC_SIZE_4WORDS, rssi_cal: "RssiCal | None" = None,
) -> Optional[RxFrame]:
    """Decode one bulk-IN URB → RxFrame, or None if malformed.

    Returns None when:
      * URB shorter than RXINFO + RXWI + RXD
      * rx_pkt_len from RXINFO is 0 or exceeds the URB
      * MPDU byte count from RXWI is < 4 (can't even have an FCS to strip)
    """
    min_len = RXINFO_DESC_SIZE + rxwi_size + RXD_DESC_SIZE
    if len(buf) < min_len:
        return None

    # RXINFO_W0
    rxinfo_w0 = struct.unpack_from("<I", buf, 0)[0]
    rx_pkt_len = rxinfo_w0 & RXINFO_W0_USB_DMA_RX_PKT_LEN
    if rx_pkt_len == 0 or rx_pkt_len + RXINFO_DESC_SIZE > len(buf):
        return None

    # RXWI: 3 words we care about (W0, W1, W2)
    rxwi_off = RXINFO_DESC_SIZE
    rxwi_w0 = struct.unpack_from("<I", buf, rxwi_off + 0)[0]
    rxwi_w1 = struct.unpack_from("<I", buf, rxwi_off + 4)[0]
    rxwi_w2 = struct.unpack_from("<I", buf, rxwi_off + 8)[0]

    mpdu_len = (rxwi_w0 & RXWI_W0_MPDU_TOTAL_BYTE_COUNT) >> 16
    if mpdu_len < 4:
        return None

    mcs = (rxwi_w1 & RXWI_W1_MCS) >> 16

    # RXD trails after rx_pkt_len bytes (counted from start of RXWI).
    rxd_off = RXINFO_DESC_SIZE + rx_pkt_len
    if rxd_off + RXD_DESC_SIZE > len(buf):
        return None
    rxd_w0 = struct.unpack_from("<I", buf, rxd_off)[0]
    crc_error = bool(rxd_w0 & RXD_W0_CRC_ERROR)
    l2pad = bool(rxd_w0 & RXD_W0_L2PAD)

    # 802.11 frame starts right after RXWI. Two layout quirks:
    #
    #  * L2 pad: the hw inserts 2 alignment bytes between the MAC header
    #    and the body when the header length isn't 4-aligned -- every
    #    QoS-Data frame (26-byte header), which is what EAPOL rides on.
    #    RXD_W0_L2PAD flags it. MPDU_TOTAL_BYTE_COUNT counts the de-padded
    #    MPDU, so the pad must come out BEFORE trimming to mpdu_len:
    #    trimming first pushes the last 2 body bytes (an EAPOL M2's
    #    key_data tail) out of the window and clips the handshake.
    #    [SRC] rt2800usb.c:565, rt2x00queue.c rt2x00queue_remove_l2pad.
    #  * FCS: the chip already strips it for these frames, so mpdu_len is
    #    the full header+body with no trailing CRC to remove.
    frame_start = RXINFO_DESC_SIZE + rxwi_size
    body = buf[frame_start:]
    if l2pad and len(body) >= 2:
        hdrlen = _ieee80211_hdrlen(body[0], body[1])
        if 0 < hdrlen <= len(body) - 2:
            body = body[:hdrlen] + body[hdrlen + 2:]
    if len(body) < mpdu_len:
        return None
    mpdu = bytes(body[:mpdu_len])

    rssi = _agc_to_rssi(rxwi_w2, rssi_cal or _DEFAULT_RSSI_CAL)

    return RxFrame(
        mpdu=mpdu,
        rssi_dbm=rssi,
        mcs=mcs,
        has_fcs_error=crc_error,
    )


def read_rx_burst(
    dev: usb.core.Device,
    ep: int,
    *,
    max_size: int = 16384,
    timeout_ms: int = 100,
) -> Optional[bytes]:
    """One bulk-IN read.  Returns None on timeout, bytes on success."""
    try:
        data = dev.read(ep, max_size, timeout_ms)
        return bytes(data)
    except usb.core.USBError as e:
        err = getattr(e, "errno", None)
        if err in (110, 10060) or "timeout" in str(e).lower():
            return None
        raise
