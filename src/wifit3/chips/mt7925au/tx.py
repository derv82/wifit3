"""MT7925AU connac3 TX descriptor build (mt7925_mac_write_txwi).

``build_tx`` turns one raw 802.11 MPDU into the on-wire USB frame the mt76 xmit
path produces for a monitor-injected frame:

    [4B SDIO hdr][64B TXD][802.11 MPDU][pad]

The TXD is ``MT_SDIO_TXD_SIZE`` (64 B); ``mt7925_mac_write_txwi`` fills txwi[0..7]
(the first 32 B) and the trailing 32 B stay zero (mt7925_usb_sdio_write_txwi
memsets the whole 64 B first). We only ever inject raw 802.11 mgmt/ctrl frames
through the monitor vif, so this is the ``is_8023 == False`` path with the
injected-frame branch (info->flags & IEEE80211_TX_CTL_INJECTED) always taken; the
HW-encap (802.3) path is not on wifit3's injection graph.

Field masks/sizes come from constants.py (grepped from mt76_connac3_mac.h /
mt76_connac.h). The monitor-vif context (wcid 19, omac 0, band_idx 0xff -> TGID 3,
basic_rates_idx 15) is the fixed state mt7925/main.c:375-392 gives the monitor link.
"""
import struct

# ruff: noqa: F403, F405
from .constants import *

# 802.11 frame_control fields (IEEE Std 802.11, linux ieee80211.h). Standard, not
# mt76-specific; used to classify the MPDU the way mt7925_mac_write_txwi_80211 does.
_FCTL_FTYPE = 0x000C
_FCTL_STYPE = 0x00F0
_FTYPE_MGMT = 0x0000
_FTYPE_CTL = 0x0004
_FTYPE_DATA = 0x0008
_STYPE_QOS_DATA = 0x0080
_FCTL_TODS = 0x0100
_FCTL_FROMDS = 0x0200
_FCTL_ORDER = 0x8000
_HT_CTL_LEN = 4
_QOS_CTL_LEN = 2
_STYPE_ACTION = 0x00D0
_STYPE_BEACON = 0x0080
_STYPE_DISASSOC = 0x00A0
_STYPE_DEAUTH = 0x00C0
_STYPE_BACK_REQ = 0x0080             # control subtype BlockAckReq
_QOS_CTL_TID_MASK = 0x000F         # IEEE80211_QOS_CTL_TID_MASK
_QOS_CTL_TAG1D_MASK = 0x0007       # IEEE80211_QOS_CTL_TAG1D_MASK
_QOS_CTL_ACK_POLICY_NOACK = 0x0020 # IEEE80211_QOS_CTL_ACK_POLICY_NOACK
_MIN_ACTION_SIZE = 25              # IEEE80211_MIN_ACTION_SIZE: offsetof(ieee80211_mgmt, u.action.u)
_CATEGORY_BACK = 3                 # WLAN_CATEGORY_BACK
_ACTION_ADDBA_REQ = 0              # WLAN_ACTION_ADDBA_REQ
_CATEGORY_PUBLIC = 4               # WLAN_CATEGORY_PUBLIC
_CATEGORY_PROTECTED_DUAL_OF_ACTION = 9   # WLAN_CATEGORY_PROTECTED_DUAL_OF_ACTION
_PUB_ACTION_FTM_REQUEST = 32       # WLAN_PUB_ACTION_FTM_REQUEST
_PUB_ACTION_FTM_RESPONSE = 33      # WLAN_PUB_ACTION_FTM_RESPONSE
_FTM_ACTION_CODE_END = 26          # offsetofend(struct ieee80211_mgmt, u.action.u.ftm.action_code)
# mac80211 ieee802_1d_to_ac (wme.c): an 802.1d priority's AC.
_IEEE802_1D_TO_AC = (MT_TXQ_BE, MT_TXQ_BK, MT_TXQ_BK, MT_TXQ_BE, MT_TXQ_VI, MT_TXQ_VI, MT_TXQ_VO, MT_TXQ_VO)
# Each TX queue's bulk-OUT: mt76u_ac_to_hwq for 0x7925 (usb.c:910-933), out_ep[mt76_ac_to_hwq(ac) + 1], PSD on HCCA.
TX_QUEUE_ENDPOINTS = {MT_TXQ_VO: EP_OUT_AC_VO, MT_TXQ_VI: EP_OUT_AC_VI, MT_TXQ_BE: EP_OUT_AC_BE,
                      MT_TXQ_BK: EP_OUT_AC_BK, MT_TXQ_PSD: EP_OUT_HCCA}


def _is_mgmt(fc: int) -> bool:
    return (fc & _FCTL_FTYPE) == _FTYPE_MGMT


def _is_data(fc: int) -> bool:
    return (fc & _FCTL_FTYPE) == _FTYPE_DATA


def _is_data_qos(fc: int) -> bool:
    return (fc & (_FCTL_FTYPE | _STYPE_QOS_DATA)) == (_FTYPE_DATA | _STYPE_QOS_DATA)


def ieee80211_hdrlen(fc: int) -> int:
    """802.11 MAC-header length for a frame_control (linux ieee80211_hdrlen). mgmt = 24
    (+4 with +HTC), data = 24/30 (+2 QoS, +4 HTC), ctl = 10 for ACK/CTS else 16."""
    hdrlen = 24
    if _is_data(fc):
        if (fc & (_FCTL_TODS | _FCTL_FROMDS)) == (_FCTL_TODS | _FCTL_FROMDS):
            hdrlen = 30
        if _is_data_qos(fc):
            hdrlen += _QOS_CTL_LEN
            if fc & _FCTL_ORDER:
                hdrlen += _HT_CTL_LEN
        return hdrlen
    if _is_mgmt(fc):
        if fc & _FCTL_ORDER:
            hdrlen += _HT_CTL_LEN
        return hdrlen
    if (fc & _FCTL_FTYPE) == 0x0004:                 # control frame
        return 10 if (fc & 0x00E0) == 0x00C0 else 16  # ACK/CTS are 10, else 16
    return hdrlen


def skb_priority(mpdu: bytes) -> int:
    """The skb->priority mac80211 gives an injected frame (wme.c:117-145 ieee80211_select_queue_80211)."""
    fc = struct.unpack_from("<H", mpdu, 0)[0]
    if not _is_data(fc):
        return 7
    if not _is_data_qos(fc):
        return 0
    four_address = (fc & (_FCTL_TODS | _FCTL_FROMDS)) == (_FCTL_TODS | _FCTL_FROMDS)
    return mpdu[30 if four_address else 24] & _QOS_CTL_TAG1D_MASK   # ieee80211_get_qos_ctl


def select_tx_queue(mpdu: bytes) -> int:
    """The mt76 TX queue of an injected frame: its mac80211 AC (ieee802_1d_to_ac[skb_priority]), or MT_TXQ_PSD for
    a non-bufferable or deauth mgmt/ctl frame (tx.c:616-623 mt76_txq_schedule_pending_wcid, MT_DRV_HW_MGMT_TXQ)."""
    fc = struct.unpack_from("<H", mpdu, 0)[0]
    if _is_data(fc):
        return _IEEE802_1D_TO_AC[skb_priority(mpdu)]
    # ieee80211_is_bufferable_mmpdu (include/linux/ieee80211.h)
    stype = fc & _FCTL_STYPE
    if not _is_mgmt(fc) or stype == _STYPE_DEAUTH:
        return MT_TXQ_PSD
    if stype == _STYPE_DISASSOC:
        return _IEEE802_1D_TO_AC[skb_priority(mpdu)]
    if stype != _STYPE_ACTION:
        return MT_TXQ_PSD
    if (len(mpdu) >= _FTM_ACTION_CODE_END
            and mpdu[24] in (_CATEGORY_PUBLIC, _CATEGORY_PROTECTED_DUAL_OF_ACTION)
            and mpdu[25] in (_PUB_ACTION_FTM_REQUEST, _PUB_ACTION_FTM_RESPONSE)):
        return MT_TXQ_PSD
    return _IEEE802_1D_TO_AC[skb_priority(mpdu)]


def build_txwi(mpdu: bytes, *, qid: int, wcid_idx: int, omac_idx: int = MON_TX_OMAC_IDX,
               band_idx: int = MON_TX_BAND_IDX, wmm_idx: int = MON_TX_WMM_IDX,
               rate_idx: int = MON_TX_RATE_IDX) -> bytes:
    """Port of mt7925_mac_write_txwi (+ _write_txwi_80211) for a monitor-injected raw
    802.11 frame. Returns the 64-byte connac3 TXD (txwi[0..7] filled, [8..15] zero).

    ``mpdu`` is the bare 802.11 frame (no descriptor, no pad). The injected-frame branch
    reads the 802.11 sequence from ``mpdu``'s seq_ctrl. Only that (and, for WEP, the IV)
    legitimately differs run-to-run."""
    fc = struct.unpack_from("<H", mpdu, 0)[0]
    multicast = bool(mpdu[4] & 0x01)                 # addr1 group bit

    # mt7925_mac_write_txwi:756-763; on the AC queues skb_get_queue_mapping is qid. LMAC orders the ACs in reverse.
    p_fmt = MT_TX_TYPE_SF
    if qid >= MT_TXQ_PSD:
        q_idx = MT_LMAC_ALTX0
    else:
        q_idx = wmm_idx * MT76_CONNAC_MAX_WMM_SETS + 3 - qid

    txwi = [0] * 16
    txwi[0] = (((len(mpdu) + MT_SDIO_TXD_SIZE) << 0) & MT_TXD0_TX_BYTES) \
        | ((p_fmt << 23) & MT_TXD0_PKT_FMT) \
        | ((q_idx << 25) & MT_TXD0_Q_IDX)

    txwi[1] = ((wcid_idx << 0) & MT_TXD1_WLAN_IDX) | ((omac_idx << 25) & MT_TXD1_OWN_MAC)
    if band_idx:
        txwi[1] |= (band_idx << 12) & MT_TXD1_TGID

    # mac.c:789-790. mac80211 sets IEEE80211_TX_CTL_NO_ACK for a group addr1 (tx.c:1275-1277)
    # and for an injected QoS frame with the NoAck policy (wme.c:208-211).
    txwi[3] = (TXD3_REM_TX_COUNT_UNLTD << 11) & MT_TXD3_REM_TX_COUNT
    if multicast:
        txwi[3] |= MT_TXD3_NO_ACK
    elif _is_data_qos(fc):
        four_address = (fc & (_FCTL_TODS | _FCTL_FROMDS)) == (_FCTL_TODS | _FCTL_FROMDS)
        if mpdu[30 if four_address else 24] & _QOS_CTL_ACK_POLICY_NOACK:
            txwi[3] |= MT_TXD3_NO_ACK

    txwi[5] = 0                                      # pid 0: no host TX-status reporting

    txwi[6] = MT_TXD6_DAS | ((1 << 4) & MT_TXD6_MSDU_CNT) | MT_TXD6_DIS_MAT

    # --- mt7925_mac_write_txwi_80211 ---
    tid = skb_priority(mpdu) & _QOS_CTL_TID_MASK
    if ((fc & (_FCTL_FTYPE | _FCTL_STYPE)) == (_FTYPE_MGMT | _STYPE_ACTION)
            and len(mpdu) >= _MIN_ACTION_SIZE + 1
            and mpdu[24] == _CATEGORY_BACK and mpdu[25] == _ACTION_ADDBA_REQ):
        tid = MT_TX_ADDBA
    elif _is_mgmt(fc):
        tid = MT_TX_NORMAL
    hdr_info = ieee80211_hdrlen(fc) // 2
    val = ((MT_HDR_FORMAT_802_11 << 14) & MT_TXD1_HDR_FORMAT) \
        | ((hdr_info << 16) & MT_TXD1_HDR_INFO) \
        | ((tid << 21) & MT_TXD1_TID)
    if (not _is_data(fc)) or multicast:              # mgmt/ctl/mcast -> fixed rate
        val |= MT_TXD1_FIXED_RATE
    txwi[1] |= val

    fc_type = (fc & _FCTL_FTYPE) >> 2
    fc_stype = (fc & _FCTL_STYPE) >> 4
    txwi[2] |= ((fc_type << 4) & MT_TXD2_FRAME_TYPE) | ((fc_stype << 0) & MT_TXD2_SUB_TYPE)

    if multicast:
        txwi[3] |= MT_TXD3_BCM
    if (fc & (_FCTL_FTYPE | _FCTL_STYPE)) == (_FTYPE_MGMT | _STYPE_BEACON):   # mac.c:703-704
        txwi[3] |= MT_TXD3_REM_TX_COUNT

    # injected branch (IEEE80211_TX_CTL_INJECTED): sequence comes from the MPDU itself, a
    # BlockAckReq's from its start_seq_num (mac.c:706-714). Bytes past the end read as 0.
    seqno = struct.unpack_from("<H", mpdu, 22)[0] if len(mpdu) >= 24 else 0
    if (fc & (_FCTL_FTYPE | _FCTL_STYPE)) == (_FTYPE_CTL | _STYPE_BACK_REQ):
        seqno = int.from_bytes(mpdu[18:20], "little")
    sn = (seqno >> 4) & 0xFFF                         # IEEE80211_SEQ_TO_SN
    txwi[3] |= MT_TXD3_SN_VALID | ((sn << 16) & MT_TXD3_SEQ)
    txwi[3] &= ~MT_TXD3_HW_AMSDU

    if txwi[1] & MT_TXD1_FIXED_RATE:
        txwi[6] |= (rate_idx << 16) & MT_TXD6_TX_RATE
        txwi[3] |= MT_TXD3_BA_DISABLE

    return struct.pack("<16I", *(w & 0xFFFFFFFF for w in txwi))


def build_tx(mpdu: bytes, *, qid: int, wcid_idx: int, **kw) -> bytes:
    """Full USB TX frame for one MPDU: [4B SDIO hdr][64B TXD][MPDU][pad].

    SDIO hdr tx_bytes = skb->len at add-hdr time = TXD + MPDU (USB, no pad, no hdr);
    pad = round_up(4+TXD+MPDU, 4) - that, +4 for USB (mt7925_usb_sdio_tx_prepare_skb)."""
    txd = build_txwi(mpdu, qid=qid, wcid_idx=wcid_idx, **kw)
    body = txd + mpdu
    hdr = struct.pack("<I", len(body) & MT792x_SDIO_HDR_TX_BYTES)   # pkt_type 0
    frame = hdr + body
    pad = ((len(frame) + 3) & ~3) - len(frame) + 4
    return frame + b"\x00" * pad
