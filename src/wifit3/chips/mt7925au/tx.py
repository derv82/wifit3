"""MT7925AU connac3 TX descriptor build (mt7925_mac_write_txwi).

``build_tx`` turns one raw 802.11 MPDU into the on-wire USB frame the mt76 xmit
path produces for a monitor-injected frame:

    [4B SDIO hdr][64B TXD][802.11 MPDU][pad]

The TXD is ``MT_SDIO_TXD_SIZE`` (64 B); ``mt7925_mac_write_txwi`` fills txwi[0..7]
(the first 32 B) and the trailing 32 B stay zero (mt7925_usb_sdio_write_txwi
memsets the whole 64 B first). We only ever inject raw 802.11 mgmt/ctrl frames
through the monitor vif, so this is the ``is_8023 == False`` path with the
injected-frame branch (info->flags & IEEE80211_TX_CTL_INJECTED) always taken; the
HW-encap (802.3) and data-AC queue paths are not on wifit3's injection graph.

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
_FTYPE_DATA = 0x0008
_STYPE_QOS_DATA = 0x0080
_FCTL_TODS = 0x0100
_FCTL_FROMDS = 0x0200
_FCTL_ORDER = 0x8000
_HT_CTL_LEN = 4
_QOS_CTL_LEN = 2


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


def build_txwi(mpdu: bytes, *, wcid_idx: int, omac_idx: int = MON_TX_OMAC_IDX,
               band_idx: int = MON_TX_BAND_IDX, rate_idx: int = MON_TX_RATE_IDX,
               no_ack: bool = True) -> bytes:
    """Port of mt7925_mac_write_txwi (+ _write_txwi_80211) for a monitor-injected raw
    802.11 frame. Returns the 64-byte connac3 TXD (txwi[0..7] filled, [8..15] zero).

    ``mpdu`` is the bare 802.11 frame (no descriptor, no pad). The injected-frame
    branch reads the 802.11 sequence from ``mpdu``'s seq_ctrl; only that (and, for WEP,
    the IV) legitimately differs run-to-run."""
    fc = struct.unpack_from("<H", mpdu, 0)[0]
    multicast = bool(mpdu[4] & 0x01)                 # addr1 group bit

    # q_idx branch: a raw mgmt/PSD injection maps to MT_TXQ_PSD -> ALTX0, USB short-format
    # (mt7925_mac_write_txwi:758-760). Data-AC TX is out of wifit3's scope.
    p_fmt = MT_TX_TYPE_SF
    q_idx = MT_LMAC_ALTX0

    txwi = [0] * 16
    txwi[0] = (((len(mpdu) + MT_SDIO_TXD_SIZE) << 0) & MT_TXD0_TX_BYTES) \
        | ((p_fmt << 23) & MT_TXD0_PKT_FMT) \
        | ((q_idx << 25) & MT_TXD0_Q_IDX)

    txwi[1] = ((wcid_idx << 0) & MT_TXD1_WLAN_IDX) | ((omac_idx << 25) & MT_TXD1_OWN_MAC)
    if band_idx:
        txwi[1] |= (band_idx << 12) & MT_TXD1_TGID

    txwi[3] = (TXD3_REM_TX_COUNT_UNLTD << 11) & MT_TXD3_REM_TX_COUNT
    if no_ack:
        txwi[3] |= MT_TXD3_NO_ACK

    txwi[5] = 0                                      # pid 0: NO_ACK injection wants no TX status

    txwi[6] = MT_TXD6_DAS | ((1 << 4) & MT_TXD6_MSDU_CNT) | MT_TXD6_DIS_MAT

    # --- mt7925_mac_write_txwi_80211 ---
    tid = MT_TX_NORMAL if _is_mgmt(fc) else 0        # mgmt -> NORMAL(0); ADDBA path n/a here
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

    # injected branch (IEEE80211_TX_CTL_INJECTED): sequence comes from the MPDU itself.
    seqno = struct.unpack_from("<H", mpdu, 22)[0] if len(mpdu) >= 24 else 0
    sn = (seqno >> 4) & 0xFFF                         # IEEE80211_SEQ_TO_SN
    txwi[3] |= MT_TXD3_SN_VALID | ((sn << 16) & MT_TXD3_SEQ)
    txwi[3] &= ~MT_TXD3_HW_AMSDU

    if txwi[1] & MT_TXD1_FIXED_RATE:
        txwi[6] |= (rate_idx << 16) & MT_TXD6_TX_RATE
        txwi[3] |= MT_TXD3_BA_DISABLE

    return struct.pack("<16I", *(w & 0xFFFFFFFF for w in txwi))


def build_tx(mpdu: bytes, *, wcid_idx: int, **kw) -> bytes:
    """Full USB TX frame for one MPDU: [4B SDIO hdr][64B TXD][MPDU][pad].

    SDIO hdr tx_bytes = skb->len at add-hdr time = TXD + MPDU (USB, no pad, no hdr);
    pad = round_up(4+TXD+MPDU, 4) - that, +4 for USB (mt7925_usb_sdio_tx_prepare_skb)."""
    txd = build_txwi(mpdu, wcid_idx=wcid_idx, **kw)
    body = txd + mpdu
    hdr = struct.pack("<I", len(body) & MT792x_SDIO_HDR_TX_BYTES)   # pkt_type 0
    frame = hdr + body
    pad = ((len(frame) + 3) & ~3) - len(frame) + 4
    return frame + b"\x00" * pad
