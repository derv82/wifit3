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
_FCTL_ORDER     = 0x8000
_FTYPE_MGMT     = 0
_FTYPE_CTL      = 1
_FTYPE_DATA     = 2
_FTYPE_EXT      = 3
_STYPE_QOS_DATA = 0x0080   # data-subtype bit that marks a QoS frame
_STYPE_ACTION   = 0x00D0
_STYPE_DISASSOC = 0x00A0
_STYPE_DEAUTH   = 0x00C0
_STYPE_BACK_REQ = 0x0080   # control subtype BlockAckReq
_QOS_CTL_LEN    = 2
_HT_CTL_LEN     = 4
_QOS_CTL_TID_MASK = 0x000F     # IEEE80211_QOS_CTL_TID_MASK
_QOS_CTL_TAG1D_MASK = 0x0007   # IEEE80211_QOS_CTL_TAG1D_MASK
_QOS_CTL_ACK_POLICY_NOACK = 0x0020   # IEEE80211_QOS_CTL_ACK_POLICY_NOACK
_BAR_CTRL_TID_INFO_SHIFT = 12  # IEEE80211_BAR_CTRL_TID_INFO_MASK 0xf000
_MIN_ACTION_SIZE = 25          # IEEE80211_MIN_ACTION_SIZE: offsetof(ieee80211_mgmt, u.action.u)
_CATEGORY_BACK = 3             # WLAN_CATEGORY_BACK
_ACTION_ADDBA_REQ = 0          # WLAN_ACTION_ADDBA_REQ
_CATEGORY_PUBLIC = 4           # WLAN_CATEGORY_PUBLIC
_CATEGORY_PROTECTED_DUAL_OF_ACTION = 9   # WLAN_CATEGORY_PROTECTED_DUAL_OF_ACTION
_PUB_ACTION_FTM_REQUEST = 32   # WLAN_PUB_ACTION_FTM_REQUEST
_PUB_ACTION_FTM_RESPONSE = 33  # WLAN_PUB_ACTION_FTM_RESPONSE
_FTM_ACTION_CODE_END = 26      # offsetofend(struct ieee80211_mgmt, u.action.u.ftm.action_code)
_RFC1042_HEADER = b"\xaa\xaa\x03\x00\x00\x00"   # rfc1042_header (net/wireless/util.c:2681)
_ETH_P_PAE = b"\x88\x8e"       # ETH_P_PAE, big-endian as skb->protocol
# mac80211 ieee802_1d_to_ac (wme.c): an 802.1d priority's AC.
_IEEE802_1D_TO_AC = (MT_TXQ_BE, MT_TXQ_BK, MT_TXQ_BK, MT_TXQ_BE, MT_TXQ_VI, MT_TXQ_VI, MT_TXQ_VO, MT_TXQ_VO)
# Each TX queue's bulk-OUT: mt76u_ac_to_hwq for 0x7961 (usb.c:927-930), out_ep[mt76_ac_to_hwq(ac) + 1], PSD on HCCA.
TX_QUEUE_ENDPOINTS = {MT_TXQ_VO: EP_OUT_AC_VO, MT_TXQ_VI: EP_OUT_AC_VI, MT_TXQ_BE: EP_OUT_AC_BE,
                      MT_TXQ_BK: EP_OUT_AC_BK, MT_TXQ_PSD: EP_OUT_HCCA}


def _ffs(mask: int) -> int:
    return (mask & -mask).bit_length() - 1


def _fp(mask: int, val: int) -> int:
    """FIELD_PREP(mask, val)."""
    return (val << _ffs(mask)) & mask


def _ieee80211_hdrlen(fc: int) -> int:
    """ieee80211_hdrlen (include/linux/ieee80211.h): MAC-header length in bytes for frame-control ``fc``."""
    fc_type = (fc & _FCTL_FTYPE) >> 2
    if fc_type == _FTYPE_EXT:
        return 4
    if fc_type == _FTYPE_DATA:
        hdrlen = 24
        if (fc & (_FCTL_TODS | _FCTL_FROMDS)) == (_FCTL_TODS | _FCTL_FROMDS):
            hdrlen = 30
        if fc & _STYPE_QOS_DATA:
            hdrlen += _QOS_CTL_LEN
            if fc & _FCTL_ORDER:
                hdrlen += _HT_CTL_LEN
        return hdrlen
    if fc_type == _FTYPE_MGMT:
        if fc & _FCTL_ORDER:
            return 24 + _HT_CTL_LEN
        return 24
    # Control: ACK and CTS are 10 bytes, all others 16.
    if (fc & 0x00E0) == 0x00C0:
        return 10
    return 16


def skb_priority(frame: bytes) -> int:
    """The skb->priority mac80211 gives an injected frame (wme.c ieee80211_select_queue_80211)."""
    fc = frame[0] | (frame[1] << 8)
    if (fc & _FCTL_FTYPE) >> 2 != _FTYPE_DATA:
        return 7
    if not fc & _STYPE_QOS_DATA:
        return 0
    four_address = (fc & (_FCTL_TODS | _FCTL_FROMDS)) == (_FCTL_TODS | _FCTL_FROMDS)
    return frame[30 if four_address else 24] & _QOS_CTL_TAG1D_MASK   # ieee80211_get_qos_ctl


def select_tx_queue(frame: bytes) -> int:
    """The mt76 TX queue of an injected frame: its mac80211 AC (ieee802_1d_to_ac[skb_priority]), or MT_TXQ_PSD for a
    non-bufferable or deauth mgmt/ctl frame (mt76-source-v6.19.14 tx.c:617-623, MT_DRV_HW_MGMT_TXQ)."""
    fc = frame[0] | (frame[1] << 8)
    fc_type = (fc & _FCTL_FTYPE) >> 2
    stype = fc & _FCTL_STYPE
    if fc_type == _FTYPE_DATA:
        return _IEEE802_1D_TO_AC[skb_priority(frame)]
    # ieee80211_is_bufferable_mmpdu (include/linux/ieee80211.h)
    if fc_type != _FTYPE_MGMT or stype == _STYPE_DEAUTH:
        return MT_TXQ_PSD
    if stype == _STYPE_DISASSOC:
        return _IEEE802_1D_TO_AC[skb_priority(frame)]
    if stype != _STYPE_ACTION:
        return MT_TXQ_PSD
    if (len(frame) >= _FTM_ACTION_CODE_END
            and frame[24] in (_CATEGORY_PUBLIC, _CATEGORY_PROTECTED_DUAL_OF_ACTION)
            and frame[25] in (_PUB_ACTION_FTM_REQUEST, _PUB_ACTION_FTM_RESPONSE)):
        return MT_TXQ_PSD
    return _IEEE802_1D_TO_AC[skb_priority(frame)]


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
             wcid_idx: int = MT792x_WTBL_RESERVED,
             retry_count: int = 15,   # mac80211 injected-frame REM_TX_COUNT [captures_mt7921u]
             pid: int = 0) -> tuple[bytes, int]:
    """Build the USB bulk-OUT bytes for raw 802.11 ``frame`` and pick its endpoint.

    Returns ``(wire_bytes, endpoint)``: the frame goes on its mt76 TX queue (select_tx_queue)
    and that queue's bulk-OUT. The monitor vif uses the reserved wcid, the sequence number
    comes from the frame, and the rate is the band's lowest basic rate.

    ``retry_count`` fills MT_TXD3_REM_TX_COUNT (the HW ACK-based retry limit). A frame that
    requests an ACK (NO_ACK clear) is retransmitted up to ``retry_count`` times until the
    recipient ACKs; a group addr1 or a QoS NoAck policy sets NO_ACK. That HW retry is
    inject_frame's only retransmission.
    """
    # ieee80211_monitor_start_xmit drops a frame shorter than its header.
    if len(frame) < 2 or len(frame) < _ieee80211_hdrlen(frame[0] | (frame[1] << 8)):
        raise ValueError(f"802.11 frame too short to inject: {len(frame)} bytes")

    fc = frame[0] | (frame[1] << 8)
    # The C reads hdr->seq_ctrl past the end of a frame shorter than 24 bytes; 0 here.
    seq_ctrl = 0
    if len(frame) >= 24:
        seq_ctrl = frame[22] | (frame[23] << 8)
    fc_type = (fc & _FCTL_FTYPE) >> 2
    fc_stype = (fc & _FCTL_STYPE) >> 4
    is_data = fc_type == _FTYPE_DATA
    multicast = bool(frame[4] & 0x01)         # addr1 group bit

    # mt76_connac2_mac_write_txwi: PSD on LMAC ALTX0, an AC on
    # wmm_idx 0 * MT76_CONNAC_MAX_WMM_SETS + mt76_connac_lmac_mapping(ac).
    qid = select_tx_queue(frame)
    if qid == MT_TXQ_PSD:
        q_idx = MT_LMAC_ALTX0
    else:
        q_idx = 0 * MT76_CONNAC_MAX_WMM_SETS + (3 - qid)
    endpoint = TX_QUEUE_ENDPOINTS[qid]

    # mt76_connac2_mac_write_txwi_80211 (mt76_connac_mac.c:401-427): tid from skb->priority,
    # an ADDBA request's capab, or a BlockAckReq's control.
    tid = skb_priority(frame) & _QOS_CTL_TID_MASK
    seqno = seq_ctrl
    add_ba = False
    frame_kind = fc & (_FCTL_FTYPE | _FCTL_STYPE)
    if (frame_kind == _STYPE_ACTION and len(frame) >= _MIN_ACTION_SIZE + 4
            and frame[24] == _CATEGORY_BACK and frame[25] == _ACTION_ADDBA_REQ):
        capab = frame[27] | (frame[28] << 8)
        add_ba = True
        tid = (capab >> 2) & _QOS_CTL_TID_MASK
    elif frame_kind == (_FTYPE_CTL << 2) | _STYPE_BACK_REQ:
        # The C reads control and start_seq_num (:470-476) past the end of a BAR shorter than
        # 20 bytes; the missing bytes are 0 here.
        tid = int.from_bytes(frame[16:18], "little") >> _BAR_CTRL_TID_INFO_SHIFT
        seqno = int.from_bytes(frame[18:20], "little")

    # mt76_connac_mac.c:449-451. USE_MINRATE: an injected rfc1042 data frame whose ethertype is
    # the monitor sdata's control_port_protocol, ETH_P_PAE (mac80211 tx.c:2359-2365, :556-560).
    hdrlen = _ieee80211_hdrlen(fc)
    use_minrate = (is_data and len(frame) >= hdrlen + len(_RFC1042_HEADER) + 2
                   and frame[hdrlen:hdrlen + 6] == _RFC1042_HEADER
                   and frame[hdrlen + 6:hdrlen + 8] == _ETH_P_PAE)
    fix_rate = (not is_data) or multicast or use_minrate

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

    # mt76_connac_mac.c:564-565. mac80211 sets IEEE80211_TX_CTL_NO_ACK for a group addr1
    # (tx.c:1275-1277) and for an injected QoS frame with the NoAck policy (wme.c:208-211).
    val3 = _fp(MT_TXD3_REM_TX_COUNT, retry_count)
    if multicast:
        val3 |= MT_TXD3_NO_ACK
    elif is_data and fc & _STYPE_QOS_DATA:
        four_address = (fc & (_FCTL_TODS | _FCTL_FROMDS)) == (_FCTL_TODS | _FCTL_FROMDS)
        if frame[30 if four_address else 24] & _QOS_CTL_ACK_POLICY_NOACK:
            val3 |= MT_TXD3_NO_ACK
    # Injected frame: carry its own sequence number (SEQ_TO_SN = seqno >> 4).
    val3 |= MT_TXD3_SN_VALID | _fp(MT_TXD3_SEQ, seqno >> 4)
    txwi[3] = val3

    val5 = _fp(MT_TXD5_PID, pid)
    if pid >= MT_PACKET_ID_FIRST:
        val5 |= MT_TXD5_TX_STATUS_HOST
    if add_ba:
        val5 |= MT_TXD5_ADD_BA
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
