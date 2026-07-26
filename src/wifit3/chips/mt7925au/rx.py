"""MT7925AU connac3 RX descriptor decode.

``classify`` demuxes the bulk-IN stream the RX reader posts on EP 0x84 into MCU
responses (fed to the command-response queue) and 802.11 frames. Full MPDU decode
(descriptor strip, RSSI) lands with the operational RX milestone.

Field masks are grepped verbatim from mt76_connac3_mac.h; the rx_pkt_type enum is
from mt76.h.
"""
import struct

# ruff: noqa: F403, F405
from .constants import *

# mt76_connac3_mac.h: rxd0 PKT_TYPE GENMASK(31,27), PKT_FLAG GENMASK(19,16).
_MT_RXD0_PKT_TYPE = 0xF8000000
_MT_RXD0_PKT_FLAG = 0x000F0000
# enum rx_pkt_type (mt76.h): a RX_EVENT carrying PKT_FLAG 0x1 is a normal frame
# (PKT_TYPE_NORMAL_MCU), per mt7925_queue_rx_skb (mt7925/mac.c:1224).
_PKT_FLAG_NORMAL = 0x1


def _pkt_type(data: bytes) -> int:
    if len(data) < 4:
        return -1
    rxd0 = struct.unpack_from("<I", data, 0)[0]
    return (rxd0 & _MT_RXD0_PKT_TYPE) >> 27


def classify(data: bytes) -> str:
    """'mcu' for an MCU response / firmware event, '80211' for a received frame.
    Mirrors mt7925_queue_rx_skb: PKT_TYPE_RX_EVENT is an MCU response unless its
    PKT_FLAG is 0x1, which the firmware uses to tunnel a normal frame."""
    if len(data) < 4:
        return "80211"
    rxd0 = struct.unpack_from("<I", data, 0)[0]
    ptype = (rxd0 & _MT_RXD0_PKT_TYPE) >> 27
    flag = (rxd0 & _MT_RXD0_PKT_FLAG) >> 16
    if ptype == PKT_TYPE_RX_EVENT and flag != _PKT_FLAG_NORMAL:
        return "mcu"
    return "80211"


def decode_frame(data: bytes, antenna_mask: int):
    """Strip the connac3 RX descriptor and return (mpdu_off, mpdu_end, rssi, fcs_err),
    or None. Completed with the operational RX milestone."""
    return None
