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
    """Strip the connac3 RX descriptor (mt7925_mac_fill_rx, mt7925/mac.c:354) and return
    (mpdu_off, mpdu_end, rssi, fcs_err), or None on a malformed/too-short buffer.

    The RXD is 8 dwords, plus 4 more per present GROUP (rxd1 bits 16-19) and 24 for
    GROUP_5 inside GROUP_3. The MPDU begins at that offset + 2*remove_pad; the total RX
    byte count (rxd0 LENGTH) is where it ends (the HW already stripped the FCS). RSSI is
    RCPI0 from the GROUP_3 P-RXV word rxv[3]."""
    if len(data) < 32:
        return None
    rxd0, rxd1, rxd2, rxd3 = struct.unpack_from("<IIII", data, 0)
    length = rxd0 & MT_RXD0_LENGTH
    if length > len(data):
        length = len(data)
    fcs_err = bool(rxd3 & MT_RXD3_NORMAL_FCS_ERR)
    remove_pad = (rxd2 >> MT_RXD2_NORMAL_HDR_OFFSET_SHIFT) & MT_RXD2_NORMAL_HDR_OFFSET_MASK

    off = 8 * 4                                   # base RXD: 8 dwords
    rssi = None
    if rxd1 & MT_RXD1_NORMAL_GROUP_4:
        off += 4 * 4
    if rxd1 & MT_RXD1_NORMAL_GROUP_1:
        off += 4 * 4
    if rxd1 & MT_RXD1_NORMAL_GROUP_2:
        off += 4 * 4
    if rxd1 & MT_RXD1_NORMAL_GROUP_3:
        if off + 16 > len(data):
            return None
        v3 = struct.unpack_from("<I", data, off + 12)[0]   # rxv[3]
        rcpi = v3 & MT_PRXV_RCPI0
        rssi = (rcpi - 220) // 2                  # to_rssi: RCPI to dBm
        off += 4 * 4
        if rxd1 & MT_RXD1_NORMAL_GROUP_5:
            off += 24 * 4

    mpdu_off = off + 2 * remove_pad
    if mpdu_off >= length:
        return None
    return mpdu_off, length, rssi, fcs_err
