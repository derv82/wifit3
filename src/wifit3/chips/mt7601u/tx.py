"""TX descriptor construction for MT7601U.

Ported from driver_sources/mt7601u-source-v7.2/mt7601u/tx.c (mt7601u_push_txwi,
mt7601u_tx_pktid_enc) and dma.h (struct mt76_txwi, mt7601u_dma_skb_wrap).

A transmit buffer is

    | 4B DMA info | 20B txwi | frame | 2B header pad? | 4B trailer |

The txwi carries the length, the station slot and the ACK policy; the DMA info word
carries the transfer length, destination port and packet type.

The byte layout is verified op-for-op against a recorded kernel-driver injection by
scripts/chips/mt7601u/verify_tx.py.
"""
from __future__ import annotations

from .constants import (
    DMA_PACKET,
    MT_QSEL_EDCA,
    MT_QSEL_MGMT,
    MT_TXWI_ACK_CTL_NSEQ,
    MT_TXWI_ACK_CTL_REQ,
    MT_TXWI_LEN_BYTE_CNT,
    MT_TXWI_LEN_PKTID,
    MT_TXWI_RATE_MCS,
    MT_TXD_INFO_D_PORT,
    MT_TXD_PKT_INFO_80211,
    MT_TXD_PKT_INFO_QSEL,
    MT_TXD_PKT_INFO_WIV,
    MT_TXD_INFO_LEN,
    MT_TXD_INFO_TYPE,
    WLAN_PORT,
    _field_prep,
)
from .rx import _hdrlen_from_buf

TXWI_LEN = 20
"""sizeof(struct mt76_txwi): packed, so 20 on the wire despite the 4-byte alignment."""

DMA_INFO_LEN = 4
"""dma.h MT_DMA_HDR_LEN."""
TRAILER_LEN = 4
"""dma.h: the zero trailer each wrapped packet ends with."""

TX_NO_STATION = 0xFF
"""init.c:590 -- dev->mon_wcid->idx. tx.c sends monitor injection through it, so a
broadcast frame addresses no station. WCID 0 is a real station and must not be used."""

TX_RING_ENTRIES = 64
"""mt7601u.h:81 N_TX_ENTRIES. Matched rather than trimmed so a replay divergence
means a porting error rather than a ring-shape mismatch."""

TX_QUEUE_COUNT = 6
"""usb.h:35-41 -- INBAND_CMD, AC_BK, AC_BE, AC_VI, AC_VO, HCCA."""

TX_QUEUE_INBAND_CMD = 0
TX_QUEUE_AC_BK = 1
TX_QUEUE_AC_BE = 2
TX_QUEUE_AC_VI = 3
TX_QUEUE_AC_VO = 4
TX_QUEUE_HCCA = 5

TX_QUEUE_INJECT = TX_QUEUE_AC_VO
"""The OUT index tx.c lands an injected frame on. Its labels are a trap: an
unclassified skb has mac80211 queue 0, q2hwq(0) is 0 ^ 3 = 3, and q2ep(3) is
3 + 1 = 4. Index 4 is named AC_VO but carries queue 0, and index 2 named AC_BE
carries qid 1. Sending on 2 reaches the chip and reports TX success while
nothing is modulated."""


def dma_queue_for_endpoint(endpoint: int) -> int:
    """dma.c ep2dmaq: MT_QSEL_MGMT for endpoint index 5, MT_QSEL_EDCA otherwise.

    This is not the endpoint index. QSEL is a 2-bit field naming the hardware
    queue, and the endpoint index only selects which one in a narrow way.
    """
    if not 0 <= endpoint < TX_QUEUE_COUNT:
        raise ValueError(
            f"endpoint index {endpoint} outside the {TX_QUEUE_COUNT} OUT endpoints")
    return MT_QSEL_MGMT if endpoint == TX_QUEUE_HCCA else MT_QSEL_EDCA

RATE_CONTROLLED = -1
"""tx.c:151 -- `rate->idx < 0` means take the WCID's stored rate instead of a chosen one."""

WCID_STORED_RATE = 0
"""The `wcid->tx_rate` RATE_CONTROLLED resolves to. The monitor WCID (init.c:589) never
has one set, and every captured monitor txwi carries rate_ctl 0."""

PROBE_PKTID_BASE = 8
"""tx.c mt7601u_tx_pktid_enc: is_probe adds 8, giving probe frames their own range."""

MIN_FRAME_LEN = 10
"""The shortest 802.11 frame -- a control frame with no addresses."""


def packet_id(rate: int, is_probe: bool) -> int:
    """tx.c mt7601u_tx_pktid_enc.

    PKT_ID 0 disables status reporting, but the encoding needs 16 values for 8 MCS
    indices times probe-or-not, so only 15 are usable. MCS7 probe and MCS0 probe
    collide at 9 and the latter folds back one step.
    """
    if not 0 <= rate < 8:
        raise ValueError(f"rate index {rate} outside the 3-bit MCS field")
    encoded = (rate + 1) + (PROBE_PKTID_BASE if is_probe else 0)
    if is_probe and rate == 7:
        return encoded - 7
    return encoded


def header_pad_len(hdr_len: int) -> int:
    """The pad that follows the MAC header so the body starts 4-byte aligned.

    Valid header lengths are 10, 16, 20, 24, 26 and 30, which are only ever 0 or 2
    modulo 4, so the pad either aligns the body or is omitted. tx.c reserves the two
    head bytes only when hdr_len % 4 is non-zero.
    """
    return 2 if hdr_len % 4 else 0


def build_txwi(*, ack: bool = False, wcid: int = TX_NO_STATION, length: int = 0, rate: int = 0,
               assign_seq: bool = False, is_probe: bool = False) -> bytes:
    """Build the 20-byte txwi.

    ``rate`` is the rate_ctl value, or RATE_CONTROLLED for the WCID's stored rate.
    ``assign_seq`` sets MT_TXWI_ACK_CTL_NSEQ, which is the bit asking the MAC to stamp
    the 802.11 sequence number (rt2800.h:3097; tx.c:160 sets it for ASSIGN_SEQ). Clear
    -- the kernel's own monitor form -- means the MPDU's own seq_ctrl is transmitted, so
    the caller has to supply it; see MT7601UDriver._stamp_tx_seq.
    """
    if not 0 <= wcid <= 0xFF:
        raise ValueError(f"wcid {wcid} does not fit the u8 slot")
    if not 0 <= length <= C_MAX_BYTE_CNT:
        raise ValueError(f"frame length {length} exceeds the 12-bit byte count")
    if rate != RATE_CONTROLLED and not 0 <= rate <= MT_TXWI_RATE_MCS:
        raise ValueError(f"rate_ctl {rate} outside MT_TXWI_RATE_MCS")

    flags = 0                                   # tx.c memsets, then sets AMPDU only
    rate_ctl = WCID_STORED_RATE if rate == RATE_CONTROLLED else rate
    ack_ctl = 0
    if ack:
        ack_ctl |= MT_TXWI_ACK_CTL_REQ
    if assign_seq:
        ack_ctl |= MT_TXWI_ACK_CTL_NSEQ
    pkt_id = packet_id(rate_ctl & 0x7, is_probe)        # tx.c:183
    len_ctl = _field_prep(MT_TXWI_LEN_BYTE_CNT, length) | _field_prep(MT_TXWI_LEN_PKTID, pkt_id)

    return (flags.to_bytes(2, "little")
            + rate_ctl.to_bytes(2, "little")
            + bytes([ack_ctl, wcid & 0xFF])
            + len_ctl.to_bytes(2, "little")
            + bytes(8)                          # iv, eiv: unencrypted
            + bytes(2)                          # aid, txstream
            + bytes(2))                         # ctl: no power adjustment


C_MAX_BYTE_CNT = 0xFFF
"""MT_TXWI_LEN_BYTE_CNT is 12 bits."""


def build_tx_dma(frame: bytes, *, ack: bool = False, wcid: int = TX_NO_STATION,
                 rate: int = 0, assign_seq: bool = False,
                 queue: int = TX_QUEUE_INJECT) -> bytes:
    """Wrap ``frame`` for transmission: DMA info, txwi, header pad, zero trailer.

    ``length`` in the txwi is the unpadded frame length; tx.c captures pkt_len before
    mt76_insert_hdr_pad adds the alignment bytes.

    ``queue`` is the OUT endpoint index (it selects the pipe in dma.c), and
    MT_TXD_PKT_INFO_QSEL is derived from it by dma_queue_for_endpoint rather than
    copied from it -- QSEL is 2 bits naming a hardware queue, not a USB endpoint.
    The 80211 and WIV flags come from mt7601u_dma_enqueue_tx: the payload is a
    complete frame, and the monitor WCID carries hw_key_idx -1, so the frame is
    sent without a per-packet IV.
    """
    if not 0 <= queue < TX_QUEUE_COUNT:
        raise ValueError(f"queue {queue} outside the {TX_QUEUE_COUNT} OUT endpoints")
    if queue == TX_QUEUE_INBAND_CMD:
        # dma.c:355 q2ep returns qid + 1, so a frame endpoint is never 0. Index 0 is the
        # MCU's command pipe, and frame bytes on it stop the receive stream.
        raise ValueError("endpoint 0 is the MCU inband command pipe, not a frame queue")
    if len(frame) < MIN_FRAME_LEN:
        raise ValueError(f"frame of {len(frame)} bytes is too short to be 802.11")
    hdr_len = _hdrlen_from_buf(frame)
    if not hdr_len:
        raise ValueError("frame control names a header longer than the frame")
    pad = b"\x00" * header_pad_len(hdr_len)
    body = frame[:hdr_len] + pad + frame[hdr_len:]

    txwi = build_txwi(ack=ack, wcid=wcid, length=len(frame), rate=rate,
                      assign_seq=assign_seq)
    # dma.h: the length field carries round_up(txwi + frame, 4) -- it excludes the
    # DMA info word and the zero trailer, which the capture confirms (LEN 48 on a
    # 56-byte transfer). skb_put_padto then appends 4 more zero bytes.
    payload = txwi + body
    pad_to_4 = -(len(payload)) % 4
    payload += bytes(pad_to_4)
    info_len = len(payload)
    info = (_field_prep(MT_TXD_INFO_LEN, info_len)
            | _field_prep(MT_TXD_INFO_D_PORT, WLAN_PORT)
            | _field_prep(MT_TXD_INFO_TYPE, DMA_PACKET)
            | _field_prep(MT_TXD_PKT_INFO_QSEL, dma_queue_for_endpoint(queue))
            | MT_TXD_PKT_INFO_80211 | MT_TXD_PKT_INFO_WIV)
    return info.to_bytes(4, "little") + payload + bytes(TRAILER_LEN)


def stamp_seq_ctrl(frame: bytearray, seqno: int) -> int:
    """Stamp an incrementing 802.11 sequence number into seq_ctrl (bytes 22-23),
    preserving the fragment number; return the advanced seqno.

    build_txwi leaves MT_TXWI_ACK_CTL_NSEQ clear, matching the kernel's monitor form, and
    that bit is what asks the MAC to assign the number (rt2800.h:3097). Clear, the MPDU's
    own seq_ctrl goes out -- and the campaigns build it as 0, so without this every frame
    of a burst shares sequence 0 and a receiver's duplicate filter drops all but the
    first. The number lives in bits [4:15], so one step is 0x10.
    """
    if len(frame) < 24:               # control frames carry no seq_ctrl
        return seqno
    frag = frame[22] & 0x0F
    if frag == 0:
        seqno = (seqno + 0x10) & 0xFFF0
    sctl = seqno | frag
    frame[22] = sctl & 0xFF           # seq_ctrl is __le16
    frame[23] = (sctl >> 8) & 0xFF
    return seqno
