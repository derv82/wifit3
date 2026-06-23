"""
MT7921AU RX path — connac2 RX descriptor decode + EP-0x84 demux.

EP 0x84 carries both MCU responses and 802.11 frames; the rxd0 packet type tells
them apart (mt7921_queue_rx_skb). For an 802.11 frame, mt7921_mac_fill_rx walks
the variable RXD groups (selected by rxd1) to find where the MPDU begins and pulls
the RCPI-derived signal from the P-RXV / GROUP_5 vector. Port of
mt7921/mac.c; validatable offline against captured beacons.
"""
import struct

# ruff: noqa: F403, F405
from .constants import *


def classify(data: bytes):
    """'mcu' (MCU response/event), 'frame' (802.11), or None (drop) from rxd0."""
    if len(data) < 4:
        return None
    rxd0 = struct.unpack_from("<I", data, 0)[0]
    ptype = (rxd0 >> 27) & 0x1F
    flag = (rxd0 >> 16) & 0xF
    if ptype == PKT_TYPE_RX_EVENT and flag == 0x1:
        ptype = PKT_TYPE_NORMAL_MCU
    if ptype == PKT_TYPE_RX_EVENT:
        return "mcu"
    if ptype in (PKT_TYPE_NORMAL, PKT_TYPE_NORMAL_MCU):
        return "frame"
    return None


def decode_frame(data: bytes):
    """mt7921_mac_fill_rx — return (mpdu_offset, mpdu_end, rssi, fcs_err) or None.

    ``mpdu_end`` is MT_RXD0_LENGTH — the RX byte count (RXD + MPDU, FCS already
    HW-stripped); the buffer tail past it is alignment padding. The caller slices
    data[mpdu_offset:mpdu_end] so the parsed frame ends exactly at the MPDU; not
    truncating lets the beacon IE walk over-read padding (a spurious RSN IE flips
    a WEP AP to WPA2) and corrupts WEP-ICV / WPS-HMAC / frag length math.

    Group sizes (words): group 0 = 6; +4 group_4; +4 group_1; +2 group_2;
    group_3 = 2 (P-RXV), +6+12 more when group_5. hdr_gap = words*4 +
    2*remove_pad. RSSI = max over chains 0/1 (antenna_mask 0x3) of
    to_rssi(RCPIi) = (rcpi - 220) / 2.
    """
    if len(data) < 24:
        return None
    rxd0 = struct.unpack_from("<I", data, 0)[0]
    rxd1, rxd2 = struct.unpack_from("<II", data, 4)
    if rxd1 & MT_RXD1_NORMAL_BAND_IDX:
        return None
    fcs_err = bool(rxd1 & MT_RXD1_NORMAL_FCS_ERR)
    remove_pad = (rxd2 >> MT_RXD2_NORMAL_HDR_OFFSET_SHIFT) & MT_RXD2_NORMAL_HDR_OFFSET_MASK

    words = 6
    if rxd1 & MT_RXD1_NORMAL_GROUP_4:
        words += 4
    if rxd1 & MT_RXD1_NORMAL_GROUP_1:
        words += 4
    if rxd1 & MT_RXD1_NORMAL_GROUP_2:
        words += 2

    rssi = -128
    if rxd1 & MT_RXD1_NORMAL_GROUP_3:
        rxv_word = words
        words += 2
        if rxd1 & MT_RXD1_NORMAL_GROUP_5:
            words += 6
            rxv_word = words
            words += 12
            if (rxv_word + 1) * 4 > len(data):
                return None
            v1 = struct.unpack_from("<I", data, rxv_word * 4)[0]
        else:
            if (rxv_word + 2) * 4 > len(data):
                return None
            v1 = struct.unpack_from("<I", data, (rxv_word + 1) * 4)[0]
        for shift in (0, 8):                    # antenna_mask 0x3 -> chains 0,1
            sig = (((v1 >> shift) & 0xFF) - 220) // 2
            if sig < 0:
                rssi = max(rssi, sig)

    mpdu = words * 4 + 2 * remove_pad
    mpdu_end = rxd0 & MT_RXD0_LENGTH
    if mpdu_end > len(data):
        mpdu_end = len(data)             # defensive: never slice past the buffer
    if not mpdu < mpdu_end:
        return None
    return mpdu, mpdu_end, rssi, fcs_err
