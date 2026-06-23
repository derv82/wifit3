"""
MT7921AU connac2 TX descriptor build — for raw 802.11 injection.

Port of mt7921_usb_sdio_tx_prepare_skb -> mt76_connac2_mac_write_txwi (the
802.11 path, mt76_connac_mac.c) + mt792x_skb_add_usb_sdio_hdr. Produces the exact
USB bulk-OUT bytes for a raw frame:

    [SDIO hdr 4B][connac2 TXD 64B = txwi[0..8] + zero pad][802.11 frame][pad]

Byte-verified against the aireplay TX recorded in captures_mt7921u/ (the `-0`
deauth on EP 0x09 + the `--test` null frames on EP 0x04) — verify_pcap CHECK 4.

The wire is FCS-stripped on RX; on TX the hardware appends the FCS, so the frame
passed here is the bare MPDU (no FCS). Fragmented injection is out of scope — a
single whole frame per call (FRAG stays NONE), which is all deauth/inject needs.
"""
import struct

# ruff: noqa: F403, F405
from .constants import *

# 802.11 frame-control bits we read off the injected frame.
_FCTL_FTYPE     = 0x000C   # type field (bits 2-3)
_FCTL_STYPE     = 0x00F0   # subtype field (bits 4-7)
_FCTL_TODS      = 0x0100
_FCTL_FROMDS    = 0x0200
_FCTL_MOREFRAGS = 0x0400
_FTYPE_DATA     = 2
_STYPE_QOS_DATA = 0x0080   # data-subtype bit that marks a QoS frame


def _ffs(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def _fp(mask: int, val: int) -> int:
    """FIELD_PREP(mask, val)."""
    return (val << _ffs(mask)) & mask


def _ieee80211_hdrlen(fc: int) -> int:
    """ieee80211_hdrlen — MAC-header length in bytes for frame-control ``fc``."""
    hdrlen = 24
    if ((fc & _FCTL_FTYPE) >> 2) == _FTYPE_DATA:
        if (fc & (_FCTL_TODS | _FCTL_FROMDS)) == (_FCTL_TODS | _FCTL_FROMDS):
            hdrlen += 6                       # 4-address frame
        if fc & _STYPE_QOS_DATA:
            hdrlen += 2                       # QoS control field
    return hdrlen


def _tx_rate_val(band_5ghz: bool) -> int:
    """mt76_connac2_mac_tx_rate_val for the monitor vif. With no association the
    basic-rate index resolves to 0; band adds offset 4 for 5/6 GHz. mt76_rates[0]
    is CCK 1 Mbps (hw_value 0x000), mt76_rates[4] is OFDM 6 Mbps (0x10b)."""
    hw_value = ((MT_PHY_TYPE_OFDM << 8) | 11) if band_5ghz else 0x000
    return _fp(MT_TX_RATE_IDX, hw_value & 0xFF) | _fp(MT_TX_RATE_MODE, hw_value >> 8)


def stamp_seq_ctrl(frame: bytearray, seqno: int) -> int:
    """Stamp an incrementing 802.11 sequence number into seq_ctrl (bytes 22-23),
    preserving the fragment number (low 4 bits); return the advanced seqno.

    build_tx sets the TXD's SN_VALID with the frame's seq, so the chip transmits
    exactly the sequence we provide — it does NOT auto-assign one for injected
    frames. Without this every inject reuses seq 0 and an AP dedups our multi-frame
    conversations (ChopChop, fragmentation, fake-auth) as retransmissions; single
    frames (deauth) and seq-carrying replays are unaffected — which is why those
    worked and the interactive attacks didn't. The number lives in bits [4:15], so
    one step is 0x10; a fragment burst (frag>0) reuses one sequence number.
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


def build_tx(frame: bytes, band_5ghz: bool = False,
             wcid_idx: int = MT792x_WTBL_RESERVED, no_ack: bool = True,
             pid: int = 0) -> tuple[bytes, int]:
    """Build the USB bulk-OUT bytes for raw 802.11 ``frame`` and pick its endpoint.

    Returns ``(wire_bytes, endpoint)``. Mirrors how mac80211 framed the aireplay
    injection: management/control frames take the PSD path (LMAC ALTX0, fixed
    lowest-rate, HCCA endpoint 0x09); data frames take their AC (BE -> 0x04). The
    monitor vif uses the reserved wcid, the sequence number comes from the frame,
    and the rate is the band's lowest basic rate.
    """
    if len(frame) < 24:
        raise ValueError(f"802.11 frame too short to inject: {len(frame)} bytes")

    fc = frame[0] | (frame[1] << 8)
    seq_ctrl = frame[22] | (frame[23] << 8)
    fc_type = (fc & _FCTL_FTYPE) >> 2
    fc_stype = (fc & _FCTL_STYPE) >> 4
    is_data = fc_type == _FTYPE_DATA
    multicast = bool(frame[4] & 0x01)         # addr1 group bit

    # qid policy -> (q_idx in the TXD, TID, USB OUT endpoint). mac80211 puts mgmt
    # on AC_VO/PSD (LMAC ALTX0, HCCA pipe) and this monitor's injected data on BE.
    if is_data:
        q_idx = 0 * MT76_CONNAC_MAX_WMM_SETS + (3 - 2)    # wmm0 + lmac_mapping(AC_BE)
        tid, endpoint = 0, EP_OUT_DATA
    else:
        q_idx, tid, endpoint = MT_LMAC_ALTX0, 7, EP_OUT_HCCA

    # FIX_RATE for everything except unicast data (mt76_connac2_mac_write_txwi).
    fix_rate = (not is_data) or multicast

    txwi = [0] * 9
    txwi[0] = (_fp(MT_TXD0_TX_BYTES, len(frame) + MT_SDIO_TXD_SIZE)
               | _fp(MT_TXD0_PKT_FMT, MT_TX_TYPE_SF) | _fp(MT_TXD0_Q_IDX, q_idx))

    txwi[1] = (MT_TXD1_LONG_FORMAT | _fp(MT_TXD1_WLAN_IDX, wcid_idx)
               | _fp(MT_TXD1_HDR_FORMAT, MT_HDR_FORMAT_802_11)
               | _fp(MT_TXD1_HDR_INFO, _ieee80211_hdrlen(fc) // 2)
               | _fp(MT_TXD1_TID, tid))     # OWN_MAC = omac_idx 0; connac2 -> no VTA

    val2 = (_fp(MT_TXD2_FRAME_TYPE, fc_type) | _fp(MT_TXD2_SUB_TYPE, fc_stype))
    if multicast:
        val2 |= MT_TXD2_MULTICAST
    if fix_rate:
        val2 |= MT_TXD2_FIX_RATE
    # FRAG: NONE for a whole frame (morefrags=0, frag-number=0); kept per the kernel.
    morefrags = bool(fc & _FCTL_MOREFRAGS)
    first_frag = (seq_ctrl & 0x000F) == 0
    if morefrags and first_frag:
        val2 |= _fp(MT_TXD2_FRAG, MT_TX_FRAG_FIRST)
    elif morefrags and not first_frag:
        val2 |= _fp(MT_TXD2_FRAG, MT_TX_FRAG_MID)
    elif not morefrags and not first_frag:
        val2 |= _fp(MT_TXD2_FRAG, MT_TX_FRAG_LAST)
    txwi[2] = val2

    val3 = _fp(MT_TXD3_REM_TX_COUNT, 15)
    if no_ack:
        val3 |= MT_TXD3_NO_ACK
    # Injected frame: carry its own sequence number (SEQ_TO_SN = seqno >> 4).
    val3 |= MT_TXD3_SN_VALID | _fp(MT_TXD3_SEQ, seq_ctrl >> 4)
    txwi[3] = val3

    val5 = _fp(MT_TXD5_PID, pid)
    if pid >= MT_PACKET_ID_FIRST:
        val5 |= MT_TXD5_TX_STATUS_HOST
    txwi[5] = val5

    txwi[8] = _fp(MT_TXD8_L_TYPE, fc_type) | _fp(MT_TXD8_L_SUB_TYPE, fc_stype)

    if fix_rate:
        txwi[2] |= MT_TXD2_HTC_VLD       # hw won't add HTC for mgmt/ctrl
        txwi[6] = MT_TXD6_FIXED_BW | _fp(MT_TXD6_TX_RATE, _tx_rate_val(band_5ghz))
        txwi[3] |= MT_TXD3_BA_DISABLE

    txd = b"".join(struct.pack("<I", w) for w in txwi).ljust(MT_SDIO_TXD_SIZE, b"\x00")

    # SDIO/USB header: tx_bytes = skb->len after write_txwi (TXD + frame), pkt_type 0.
    skb_len = MT_SDIO_TXD_SIZE + len(frame)
    sdio = struct.pack("<I", _fp(MT792x_SDIO_HDR_TX_BYTES, skb_len))

    body = sdio + txd + frame
    pad = ((len(body) + 3) & ~3) - len(body) + 4     # round_up(len, 4) + 4 (USB)
    return body + b"\x00" * pad, endpoint
