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
import threading
from collections import deque
from dataclasses import dataclass

from . import constants as C

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
WMI_FATAL_EVENTID = 0x1003          # [SRC] wmi.h:118-126 enum wmi_event_id
HTC_MAX_TX_STATUS = 12              # [SRC] wmi.h:49
ATH9K_HTC_TXSTAT_EPID = 0xf0        # [SRC] wmi.h:67
ATH9K_HTC_TXSTAT_EPID_S = 4         # [SRC] wmi.h:68
ATH9K_HTC_TX_CLEANUP_INTERVAL = 0.050   # [SRC] htc.h:287 (50 ms)
ATH9K_HTC_TX_TIMEOUT_INTERVAL = 3.000   # [SRC] htc.h:288 (3000 ms)
ATH9K_HTC_TX_TIMEOUT_COUNT = 40     # [SRC] htc.h:290
# The endpoints get_htc_epid_queue has a queue for [SRC] htc_drv_txrx.c:131-153.
TX_QUEUE_SERVICES = (C.WMI_MGMT_SVC, C.WMI_CAB_SVC, C.WMI_DATA_BE_SVC, C.WMI_DATA_BK_SVC,
                     C.WMI_DATA_VI_SVC, C.WMI_DATA_VO_SVC)

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


@dataclass
class TxEvent:
    """``struct ath9k_htc_tx_event`` [SRC] wmi.h:142-146: a status whose frame was not queued yet."""
    cookie: int
    ts_rate: int
    count: int = 0


class Ath9kHtcTx:
    """``struct ath9k_htc_tx`` [SRC] htc.h:295-307: the TX slot bitmap, and each TX endpoint's queue
    of sent frames, as (cookie, sent time), awaiting their WMI_TXSTATUS. One lock stands for its
    tx_lock, the epid queue locks and wmi->event_lock."""

    def __init__(self, endpoints: dict[int, int]) -> None:
        self.tx_slot = bytearray(MAX_TX_BUF_NUM)
        self.epid_queues: dict[int, deque[tuple[int, float]]] = {
            endpoints[service_id]: deque() for service_id in TX_QUEUE_SERVICES if service_id in endpoints}
        self.pending_tx_events: list[TxEvent] = []
        self.tx_lock = threading.Lock()

    def tx_get_slot(self) -> int:
        """ath9k_htc_tx_get_slot [SRC] htc_drv_txrx.c:79-93; -1 for its -ENOBUFS."""
        with self.tx_lock:
            for slot in range(MAX_TX_BUF_NUM):
                if not self.tx_slot[slot]:
                    self.tx_slot[slot] = 1
                    return slot
            return -1

    def tx_clear_slot(self, slot: int) -> None:
        """ath9k_htc_tx_clear_slot [SRC] htc_drv_txrx.c:95-100."""
        with self.tx_lock:
            self.tx_slot[slot] = 0

    def txep(self, epid: int, cookie: int, timestamp: float) -> None:
        """ath9k_htc_txep [SRC] htc_drv_txrx.c:690-714 for a sent frame: queue it on its endpoint."""
        with self.tx_lock:
            self.epid_queues[epid].append((cookie, timestamp))

    def tx_failed(self, epid: int, cookie: int) -> None:
        """A frame whose bulk-OUT failed leaves its queue and frees its slot [SRC] htc_drv_main.c:890-904."""
        with self.tx_lock:
            queue = self.epid_queues[epid]
            for entry in queue:
                if entry[0] == cookie:
                    queue.remove(entry)
                    break
            self.tx_slot[cookie] = 0

    def txstatus(self, wmi_event: bytes) -> None:
        """ath9k_htc_txstatus [SRC] htc_drv_txrx.c:647-688: free each reported frame's slot, or keep
        the status for the cleanup timer when its frame is not queued yet."""
        cnt = wmi_event[0]
        if cnt > HTC_MAX_TX_STATUS:
            return
        with self.tx_lock:
            for i in range(cnt):
                cookie, ts_rate = wmi_event[1 + i * 3], wmi_event[2 + i * 3]
                if not self._tx_get_packet(cookie, ts_rate):
                    self.pending_tx_events.append(TxEvent(cookie, ts_rate))

    def cleanup_timer(self, now: float) -> None:
        """ath9k_htc_tx_cleanup_timer's body [SRC] htc_drv_txrx.c:761-803: retry the kept statuses
        (dropped after ATH9K_HTC_TX_TIMEOUT_COUNT), then free frames sent over 3 s ago."""
        with self.tx_lock:
            for event in list(self.pending_tx_events):
                if self._tx_get_packet(event.cookie, event.ts_rate):
                    self.pending_tx_events.remove(event)
                    continue
                event.count += 1
                if event.count >= ATH9K_HTC_TX_TIMEOUT_COUNT:
                    self.pending_tx_events.remove(event)
            for queue in self.epid_queues.values():
                for entry in list(queue):
                    if now > entry[1] + ATH9K_HTC_TX_TIMEOUT_INTERVAL:
                        queue.remove(entry)
                        self.tx_slot[entry[0]] = 0

    def tx_drain(self) -> None:
        """ath9k_htc_tx_drain [SRC] htc_drv_txrx.c:539-578: free every queued frame's slot and drop
        the kept statuses."""
        with self.tx_lock:
            for queue in self.epid_queues.values():
                for cookie, _ in queue:
                    self.tx_slot[cookie] = 0
                queue.clear()
            self.pending_tx_events.clear()

    def _tx_get_packet(self, cookie: int, ts_rate: int) -> bool:
        """ath9k_htc_tx_get_packet + ath9k_htc_tx_process [SRC] htc_drv_txrx.c:618-645, :445-512: unlink
        the frame ``cookie`` on the status's endpoint and free its slot; False if it is not queued."""
        queue = self.epid_queues.get((ts_rate & ATH9K_HTC_TXSTAT_EPID) >> ATH9K_HTC_TXSTAT_EPID_S)
        if queue is None:
            return False
        for entry in queue:
            if entry[0] == cookie:
                queue.remove(entry)
                self.tx_slot[cookie] = 0
                return True
        return False
