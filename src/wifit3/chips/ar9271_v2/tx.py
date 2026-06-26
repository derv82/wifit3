"""HTC TX framing for the AR9271 (ath9k_htc) — the wire format + builders for injected frames.

A TX frame on the WLAN_TX bulk pipe (EP 0x01) is, outermost first:

    [HIF stream hdr]   __le16 pkt_len, __le16 tag (0x697e)            hif_usb.c (stream mode)
    [htc_frame_hdr]    u8 endpoint_id, u8 flags, __be16 payload_len, u8 control[4]   htc_hst.h
    [tx_mgmt_hdr | tx_frame_hdr]                                      htc.h:73 / :85
    [802.11 frame]

``pkt_len`` counts everything after the HIF header; ``payload_len`` counts the tx hdr + 802.11.
Management/control frames (probe/auth/deauth/RTS — the aireplay-ng ``--test`` + deauth phases)
ride the mgmt service endpoint with ``tx_mgmt_hdr`` (8 B); data frames ride a data endpoint with
``tx_frame_hdr`` (12 B). The split is by 802.11 frame type: ``ieee80211_is_data`` -> data path,
else mgmt path [SRC] htc_drv_txrx.c:381 (ath9k_htc_tx_start) / :214 (mgmt) / :260 (data).

``driver.inject_frame`` builds these from the bare 802.11 frame mac80211 hands it (which already
carries its own sequence number); only the wrapper — and the per-frame TX-slot cookie — is ours.
"""
from __future__ import annotations

import struct

HIF_TX_STREAM_TAG = 0x697e          # [WIRE] le16 stream-mode tag on every bulk-OUT TX frame
HTC_FRAME_HDR_LEN = 8               # [SRC] htc_hst.h struct htc_frame_hdr

# data_type / frame class [SRC] htc.h:65-68
ATH9K_HTC_AMPDU = 1
ATH9K_HTC_NORMAL = 2
ATH9K_HTC_BEACON = 3
ATH9K_HTC_MGMT = 4

ATH9K_TXKEYIX_INVALID = 0xff        # [SRC] mac.h (tx_*_hdr.keyix when key_type == CLEAR)
ATH9K_KEY_TYPE_CLEAR = 0            # [SRC] hw.h

# Observed HTC service endpoint ids in this capture (assigned by the connect_service handshake):
# the mgmt service is epid 5 (tx_mgmt_hdr, 8 B), the BE/data service is epid 6 (tx_frame_hdr,
# 12 B). The htc_frame_hdr's endpoint_id (byte 0) selects the header layout. Used only to DECODE
# recorded frames (dot11_from_bulk); the build path resolves epids from the live service map.
TX_MGMT_EPID = 5
TX_DATA_EPID = 6
TX_MGMT_HDR_LEN = 8                 # [SRC] htc.h:85 struct tx_mgmt_hdr
TX_FRAME_HDR_LEN = 12               # [SRC] htc.h:73 struct tx_frame_hdr

MAX_TX_BUF_NUM = 256                # [SRC] hif_usb.h:55 (priv->tx.tx_slot bitmap width)
WMI_TXSTATUS_EVENTID = 0x1007       # [SRC] wmi.h:118-126 enum wmi_event_id

# 802.11 frame_control type field (FTYPE mask, bits 2-3) [SRC] linux/ieee80211.h
_FTYPE_MASK = 0x000c
_FTYPE_DATA = 0x0008


def dot11_from_bulk(bulk: bytes) -> bytes:
    """Strip the HIF + htc_frame_hdr + tx header off a recorded bulk-OUT frame, leaving the bare
    802.11 frame (what mac80211 hands ``inject_frame``). The tx-header length is selected by the
    htc endpoint_id at byte 4 (mgmt vs data service)."""
    epid = bulk[4]
    tx_hdr_len = TX_MGMT_HDR_LEN if epid == TX_MGMT_EPID else TX_FRAME_HDR_LEN
    return bulk[4 + HTC_FRAME_HDR_LEN + tx_hdr_len:]


def hif_htc_wrap(epid: int, htc_payload: bytes) -> bytes:
    """The two outer headers shared by every TX frame: the HIF stream header (le16 len, le16 tag)
    and the htc_frame_hdr (epid, flags=0, be16 payload_len, control=0). ``htc_payload`` is the tx
    header + 802.11 frame."""
    htc = struct.pack(">BBH4x", epid, 0, len(htc_payload)) + htc_payload
    return struct.pack("<HH", len(htc), HIF_TX_STREAM_TAG) + htc


def is_data_frame(dot11: bytes) -> bool:
    """``ieee80211_is_data`` — frame type == DATA (covers Null and QoS-Null); control (RTS/CTS)
    and management both fall to the mgmt endpoint [SRC] htc_drv_txrx.c:381."""
    fc = struct.unpack_from("<H", dot11, 0)[0]
    return (fc & _FTYPE_MASK) == _FTYPE_DATA


def build_mgmt_tx(epid: int, dot11: bytes, cookie: int) -> bytes:
    """ath9k_htc_tx_mgmt [SRC] htc_drv_txrx.c:214 — prepend tx_mgmt_hdr (8 B) and wrap. For the
    monitor self-station node_idx/vif_idx/tidno/flags are 0; key_type CLEAR -> keyix INVALID."""
    hdr = struct.pack(">BBBBBBBB", 0, 0, 0, 0, ATH9K_KEY_TYPE_CLEAR, ATH9K_TXKEYIX_INVALID,
                      cookie & 0xff, 0)
    return hif_htc_wrap(epid, hdr + dot11)


def build_data_tx(epid: int, dot11: bytes, cookie: int) -> bytes:
    """ath9k_htc_tx_data [SRC] htc_drv_txrx.c:260 — prepend tx_frame_hdr (12 B) and wrap.
    data_type NORMAL, flags be32 0 (no RTS/CTS protection on the monitor self-station)."""
    hdr = struct.pack(">BBBBIBBBB", ATH9K_HTC_NORMAL, 0, 0, 0, 0, ATH9K_KEY_TYPE_CLEAR,
                      ATH9K_TXKEYIX_INVALID, cookie & 0xff, 0)
    return hif_htc_wrap(epid, hdr + dot11)


def txstatus_cookies(event_body: bytes) -> list[int]:
    """Decode a WMI_TXSTATUS event body (wmi_event_txstatus: u8 cnt, then cnt x [cookie, ts_rate,
    ts_flags]) into its freed cookies — ath9k_htc_txstatus [SRC] htc_drv_txrx.c:647, wmi.h:70-79."""
    if not event_body:
        return []
    cnt = event_body[0]
    return [event_body[1 + i * 3] for i in range(cnt) if 1 + i * 3 < len(event_body)]


class TxSlots:
    """``priv->tx.tx_slot`` [SRC] htc.h:306 — the TX cookie bitmap. ``get`` is
    ath9k_htc_tx_get_slot [SRC] htc_drv_txrx.c:79 (find_first_zero_bit + set); ``clear`` is
    ath9k_htc_tx_clear_slot [SRC] :95, driven by the WMI_TXSTATUS completion [SRC] :514. The
    kernel gates the clear on the frame's skb being queued by the bulk-OUT URB completion
    (else the event waits in pending_tx_events for the 50 ms cleanup timer); on the wire the
    status always trails its own URB, so the queued check holds and the clear is immediate —
    verified equivalent against all three pcaps."""

    def __init__(self) -> None:
        self._slot = bytearray(MAX_TX_BUF_NUM)

    def get(self) -> int:
        slot = next((i for i in range(MAX_TX_BUF_NUM) if not self._slot[i]), -1)
        if slot < 0:
            raise RuntimeError("ar9271: no free TX slot (ENOBUFS)")
        self._slot[slot] = 1
        return slot

    def clear(self, slot: int) -> None:
        if 0 <= slot < MAX_TX_BUF_NUM:
            self._slot[slot] = 0
