"""RX descriptor decode and RX-buffer iteration for MT7601U.

Ported from driver_sources/mt7601u-source-v7.2/mt7601u/dma.c (mt7601u_rx_next_seg_len,
mt7601u_rx_process_seg, mt7601u_rx_skb_from_seg) and the descriptor's ctl-field consumption
in mac.c (mt76_mac_process_rx).

A bulk-IN buffer holds one or more chained segments. Each segment is

    | 4B DMA hdr | 28B rxwi | MPDU_LEN payload | 4B FCE |

with the 4-byte DMA header carrying the segment's own length. The rxwi's `ctl` word carries
the authoritative MPDU length, which is what the frame decode uses -- the payload may be
longer (FCS and trailing pad).
"""
from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    MT_RXD_INFO_TYPE,
    MT_RXINFO_CRCERR,
    MT_RXINFO_L2PAD,
    MT_RXWI_ANT_AUX_LNA,
    MT_RXWI_CTL_MPDU_LEN,
    MT_RXWI_GAIN_RSSI_LNA_ID,
    MT_RXWI_GAIN_RSSI_VAL,
    MT_RXWI_RATE_BW,
    _field_get,
)

MT_DMA_HDR_LEN = 4
"""dma.h:13."""
MT_FCE_INFO_LEN = 4
"""dma.h:15."""
RXWI_LEN = 28
"""sizeof(struct mt7601u_rxwi) -- packed, so 28 on the wire despite the 4-byte alignment."""

# dma.c:119-125 -- a segment shorter than this cannot hold the headers, rxwi and FCE.
MIN_SEGMENT_LEN = MT_DMA_HDR_LEN + 4 + RXWI_LEN + MT_FCE_INFO_LEN
"""MT_DMA_HDRS (8) plus the descriptor and trailer. MT_RX_INFO_LEN is the second header word."""


@dataclass(frozen=True)
class RxFrame:
    """One decoded frame plus the RF measurements its descriptor carried."""
    frame: bytes
    rssi: int
    snr: int
    gain: int
    ant: int
    freq_off: int
    rate: int


def _hdrlen(frame_control: int) -> int:
    """ieee80211_hdrlen -- MAC header length in bytes for ``frame_control``.

    Ported from the kernel's call into mac80211 at dma.c:20, which resolves to
    net/wireless/util.c:455 ieee80211_hdrlen. ``frame_control`` is the full 16-bit
    value; the constants are linux/ieee80211.h:44-63 and :100.

    The previous version branched only on the DS bits and returned 10 for every
    management frame. Management MPDUs are 24 bytes, so tx.py placed the header
    pad two bytes early and corrupted Addr2, Addr3 and the sequence number of
    every injected deauth and probe request.
    """
    ftype = frame_control & 0x000C                       # IEEE80211_FCTL_FTYPE
    if ftype == 0x000C:                                   # IEEE80211_FTYPE_EXT
        return 4
    if ftype == 0x0008:                                   # IEEE80211_FTYPE_DATA
        hdrlen = 30 if frame_control & 0x0300 == 0x0300 else 24
        if frame_control & 0x008C == 0x0088:              # IEEE80211_STYPE_QOS_DATA
            hdrlen += 2
            if frame_control & 0x8000:                    # IEEE80211_FCTL_ORDER
                hdrlen += 4
        return hdrlen
    if ftype == 0x0000:                                   # IEEE80211_FTYPE_MGMT
        return 28 if frame_control & 0x8000 else 24
    return 10 if frame_control & 0x00E0 == 0x00C0 else 16  # IEEE80211_FTYPE_CTL


def _hdrlen_from_buf(data: bytes) -> int:
    """dma.c:14 ieee80211_get_hdrlen_from_buf -- 0 when the frame is too short to hold one."""
    if len(data) < 10:
        return 0
    hl = _hdrlen(int.from_bytes(data[:2], "little"))
    return hl if hl <= len(data) else 0


def next_segment_len(buf: bytes) -> int:
    """dma.c:117 -- length of the segment at the head of ``buf``, or 0 when none fits.

    The DMA header's first 16-bit word is the length the hardware chained, which excludes
    the 8-byte double header; the segment therefore spans MT_DMA_HDRS + that value.
    """
    if len(buf) < MIN_SEGMENT_LEN:                  # dma.c:123
        return 0
    dma_len = int.from_bytes(buf[:2], "little")
    if (not dma_len
            or dma_len + 8 > len(buf)
            or dma_len & 0x3
            or dma_len < MIN_SEGMENT_LEN):          # dma.c:127
        return 0
    return 8 + dma_len


def get_rssi(rate: int, ant: int, gain: int, lna_gain: int, rssi_offset: int) -> int:
    """phy.c mt7601u_phy_get_rssi -- the LNA table's compensation subtracted from a raw reading."""
    # phy.c mt7601u_phy_get_rssi static const s8 lna[2][2][3], indexed [aux_lna][bw][lna_id].
    lna = (
        ((-2, 15, 33), (0, 16, 34)),                # main LNA: bw20, bw40
        ((-2, 15, 33), (-2, 16, 34)),               # aux LNA
    )
    bw = _field_get(MT_RXWI_RATE_BW, rate)
    aux_lna = _field_get(MT_RXWI_ANT_AUX_LNA, ant)
    lna_id = _field_get(MT_RXWI_GAIN_RSSI_LNA_ID, gain)
    if lna_id:                                      # LNA id can be 0, 2 or 3.
        lna_id -= 1
    return (8 - lna[aux_lna][bw][lna_id]
            - _field_get(MT_RXWI_GAIN_RSSI_VAL, gain) - lna_gain - rssi_offset)


def decode_segment(seg: bytes, lna_gain: int = 0, rssi_offset: int = 0) -> RxFrame | None:
    """dma.c mt7601u_rx_process_seg + mt7601u_rx_skb_from_seg -- one segment to one frame.

    Returns None for the frames dma.c drops: a non-packet FCE type, an MPDU length under
    10 or past the payload, or a length that cannot fit a MAC header. Also drops
    MT_RXINFO_CRCERR, which the C leaves to MT_RX_FILTR_CFG_CRC_ERR in the MAC (main.c:117).
    """
    # dma.c:102 only dev_err_once()s on a non-pkt urb and hands the segment on regardless.
    # With no mac80211 downstream to discard it, dropping it here is the equivalent.
    if _field_get(MT_RXD_INFO_TYPE, int.from_bytes(seg[-4:], "little")):
        return None

    rxwi = seg[MT_DMA_HDR_LEN:MT_DMA_HDR_LEN + RXWI_LEN]
    payload = seg[MT_DMA_HDR_LEN + RXWI_LEN:len(seg) - MT_FCE_INFO_LEN]

    rxinfo = int.from_bytes(rxwi[0:4], "little")
    if rxinfo & MT_RXINFO_CRCERR:
        return None                                 # mac.h:54 -- the FCS did not check out

    ctl = int.from_bytes(rxwi[4:8], "little")
    true_len = _field_get(MT_RXWI_CTL_MPDU_LEN, ctl)
    if true_len < 10 or true_len > len(payload):
        return None                                 # mac.c:470, dma.c:41 -- bad frame length

    hdr_len = _hdrlen_from_buf(payload[:true_len])
    if not hdr_len:
        return None

    if rxinfo & MT_RXINFO_L2PAD:
        # The two bytes after the header are padding the hardware inserted; dma.c:51-55
        # emits the header, skips them, then emits the rest.
        frame = payload[:hdr_len] + payload[hdr_len + 2:hdr_len + 2 + true_len - hdr_len]
    else:
        frame = payload[:true_len]

    rate = int.from_bytes(rxwi[10:12], "little")   # mac.h:32 -- frag_sn, then rate
    ant, gain = rxwi[17], rxwi[18]                 # mac.h:38-39
    return RxFrame(
        frame=frame,
        rssi=get_rssi(rate, ant, gain, lna_gain, rssi_offset),
        snr=rxwi[16],                               # mac.h:37
        gain=gain,
        ant=ant,
        freq_off=rxwi[19],                          # mac.h:40
        rate=rate,
    )


def iter_frames(buf: bytes, lna_gain: int = 0, rssi_offset: int = 0):
    """dma.c mt7601u_rx_process_entry -- yield every frame chained into one bulk-IN buffer."""
    while True:
        seg_len = next_segment_len(buf)
        if not seg_len:
            return
        frame = decode_segment(buf[:seg_len], lna_gain, rssi_offset)
        if frame is not None:
            yield frame
        buf = buf[seg_len:]
