"""Port of the rtl8xxxu core.c TX path (v6.19.14) for the RTL8192FU: rtl8xxxu_tx + fill_txdesc_v2."""
from __future__ import annotations

from . import constants as C
from .core import Rtl8xxxuPriv

RTL8XXXU_TXDESC40_SIZE = 40  # sizeof(struct rtl8xxxu_txdesc40), rtl8xxxu.h
RTL8XXXU_TXDESC32_SIZE = 32  # sizeof(struct rtl8xxxu_txdesc32), rtl8xxxu.h
RTS_THRESHOLD = 2347  # hw->wiphy->rts_threshold, core.c:7864

IEEE80211_AC_VO = 0  # include/net/mac80211.h enum ieee80211_ac_numbers
IEEE80211_AC_VI = 1
IEEE80211_AC_BE = 2
IEEE80211_AC_BK = 3
ieee802_1d_to_ac = (IEEE80211_AC_BE, IEEE80211_AC_BK, IEEE80211_AC_BK, IEEE80211_AC_BE,
                    IEEE80211_AC_VI, IEEE80211_AC_VI, IEEE80211_AC_VO, IEEE80211_AC_VO)  # net/mac80211/wme.c
IEEE80211_QOS_CTL_TAG1D_MASK = 0x0007  # include/linux/ieee80211.h


def ieee80211_is_mgmt(frame: bytes) -> bool:
    return (frame[0] & 0x0C) == 0x00


def ieee80211_is_data(frame: bytes) -> bool:
    return (frame[0] & 0x0C) == 0x08


def ieee80211_is_data_qos(frame: bytes) -> bool:
    return (frame[0] & 0x8C) == 0x88


def ieee80211_is_beacon(frame: bytes) -> bool:
    return (frame[0] & 0xFC) == 0x80


def ieee80211_get_DA(frame: bytes) -> bytes:
    if frame[1] & 0x01:
        return frame[16:22]
    return frame[4:10]


def ieee80211_select_queue_80211(frame: bytes) -> int:
    """mac80211's skb queue mapping for a monitor injection (net/mac80211/wme.c), no ACM."""
    if not ieee80211_is_data(frame):
        return ieee802_1d_to_ac[7]
    if not ieee80211_is_data_qos(frame):
        return ieee802_1d_to_ac[0]
    qos_ctl = 30 if frame[1] & 0x03 == 0x03 else 24
    return ieee802_1d_to_ac[frame[qos_ctl] & IEEE80211_QOS_CTL_TAG1D_MASK]


def rtl8xxxu_80211_to_rtl_queue(queue: int) -> int:
    if queue == IEEE80211_AC_VO:
        return C.TXDESC_QUEUE_VO
    if queue == IEEE80211_AC_VI:
        return C.TXDESC_QUEUE_VI
    if queue == IEEE80211_AC_BE:
        return C.TXDESC_QUEUE_BE
    if queue == IEEE80211_AC_BK:
        return C.TXDESC_QUEUE_BK
    return C.TXDESC_QUEUE_BE


def rtl8xxxu_queue_select(frame: bytes, queue_mapping: int) -> int:
    if ieee80211_is_beacon(frame):
        return C.TXDESC_QUEUE_BEACON
    if ieee80211_is_mgmt(frame):
        return C.TXDESC_QUEUE_MGNT
    return rtl8xxxu_80211_to_rtl_queue(queue_mapping)


def _put32(desc: bytearray, offset: int, value: int) -> None:
    desc[offset:offset + 4] = (value & 0xFFFFFFFF).to_bytes(4, "little")


def _get32(desc: bytearray, offset: int) -> int:
    return int.from_bytes(desc[offset:offset + 4], "little")


_TXDW1, _TXDW2, _TXDW3, _TXDW4, _TXDW5, _CSUM, _TXDW9 = 4, 8, 12, 16, 20, 28, 36


def rtl8xxxu_fill_txdesc_v2(tx_desc40: bytearray, frame: bytes, short_preamble: bool,
                            ampdu_enable: bool, use_rts: bool, rts_rate: int, macid: int) -> None:
    """IEEE80211_TX_CTL_ASSIGN_SEQ is never set: mac80211 skips seq assignment on a monitor vif."""
    rate = 0
    _put32(tx_desc40, _TXDW1, _get32(tx_desc40, _TXDW1) | macid << C.TXDESC40_MACID_SHIFT)

    seq_number = int.from_bytes(frame[22:24], "little") >> 4

    txdw4 = rate
    if ieee80211_is_data(frame):
        txdw4 |= 0x1F << C.TXDESC40_DATA_RATE_FB_SHIFT
    _put32(tx_desc40, _TXDW9, seq_number << C.TXDESC40_SEQ_SHIFT)

    _put32(tx_desc40, _TXDW2, _get32(tx_desc40, _TXDW2) | C.TXDESC40_AGG_BREAK)

    txdw3 = _get32(tx_desc40, _TXDW3)
    if ieee80211_is_mgmt(frame):
        txdw4 = rate
        txdw3 |= C.TXDESC40_USE_DRIVER_RATE
        txdw4 |= 6 << C.TXDESC40_RETRY_LIMIT_SHIFT
        txdw4 |= C.TXDESC40_RETRY_LIMIT_ENABLE

    if short_preamble:
        _put32(tx_desc40, _TXDW5, _get32(tx_desc40, _TXDW5) | C.TXDESC40_SHORT_PREAMBLE)

    txdw4 |= rts_rate << C.TXDESC40_RTS_RATE_SHIFT

    if ampdu_enable or use_rts:
        txdw3 |= C.TXDESC40_RTS_CTS_ENABLE
        txdw3 |= C.TXDESC40_HW_RTS_ENABLE
    _put32(tx_desc40, _TXDW3, txdw3)
    _put32(tx_desc40, _TXDW4, txdw4)


def rtl8xxxu_calc_tx_desc_csum(tx_desc: bytearray) -> None:
    tx_desc[_CSUM:_CSUM + 2] = b"\x00\x00"
    csum = 0
    for i in range(0, RTL8XXXU_TXDESC32_SIZE, 2):
        csum ^= int.from_bytes(tx_desc[i:i + 2], "little")
    tx_desc[_CSUM:_CSUM + 2] = csum.to_bytes(2, "little")


def rtl8xxxu_tx(priv: Rtl8xxxuPriv, frame: bytes) -> tuple[int, bytes]:
    """(bulk-OUT endpoint, URB payload) for a monitor-injected frame: no sta, no vif, no hw_key."""
    tx_desc_size = RTL8XXXU_TXDESC40_SIZE
    if len(frame) > 65535 - tx_desc_size:
        raise ValueError(f"Trying to send over-sized skb ({len(frame)})")

    queue = rtl8xxxu_queue_select(frame, ieee80211_select_queue_80211(frame))

    tx_desc = bytearray(tx_desc_size)
    tx_desc[0:2] = len(frame).to_bytes(2, "little")
    tx_desc[2] = tx_desc_size
    if ieee80211_get_DA(frame)[0] & 0x01:
        tx_desc[3] |= C.TXDESC_BROADMULTICAST

    _put32(tx_desc, _TXDW1, queue << C.TXDESC_QUEUE_SHIFT)
    macid = 0

    ampdu_enable = False
    short_preamble = False
    use_rts = len(frame) > RTS_THRESHOLD
    rts_rate = C.DESC_RATE_24M if ampdu_enable or use_rts else 0

    rtl8xxxu_fill_txdesc_v2(tx_desc, frame, short_preamble, ampdu_enable, use_rts, rts_rate, macid)

    rtl8xxxu_calc_tx_desc_csum(tx_desc)

    csum = int.from_bytes(tx_desc[_CSUM:_CSUM + 2], "little")
    tx_desc[_CSUM:_CSUM + 2] = (~csum & 0xFFFF).to_bytes(2, "little")

    return priv.pipe_out[queue], bytes(tx_desc) + frame
