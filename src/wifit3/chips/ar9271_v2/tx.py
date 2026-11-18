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
carries its own sequence number); only the wrapper — and the TX slot in its cookie field — is ours.
"""
from __future__ import annotations

import errno
import logging
import struct
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

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
HTC_MAX_TX_STATUS = 12              # [SRC] wmi.h:49
ATH9K_HTC_TXSTAT_EPID = 0xf0        # [SRC] wmi.h:67-68
ATH9K_HTC_TXSTAT_EPID_S = 4
ATH9K_HTC_TX_CLEANUP_INTERVAL = 50  # [SRC] htc.h:287-290 (ms)
ATH9K_HTC_TX_TIMEOUT_INTERVAL = 3000  # (ms)
ATH9K_HTC_TX_TIMEOUT_COUNT = 40

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


def build_mgmt_tx(epid: int, dot11: bytes, slot: int) -> bytes:
    """ath9k_htc_tx_mgmt: prepend tx_mgmt_hdr (``cookie = slot``) and the HIF/HTC headers."""
    # node_idx, vif_idx, tidno, flags fixed at 0: the monitor vif and its self-station.
    hdr = struct.pack(">BBBBBBBB", 0, 0, 0, 0, ATH9K_KEY_TYPE_CLEAR, ATH9K_TXKEYIX_INVALID,
                      slot & 0xff, 0)
    return hif_htc_wrap(epid, hdr + dot11)


def build_data_tx(epid: int, dot11: bytes, slot: int) -> bytes:
    """ath9k_htc_tx_data: prepend tx_frame_hdr (``cookie = slot``) and the HIF/HTC headers."""
    # node_idx, vif_idx, tidno, flags fixed at 0: the monitor vif and its self-station.
    hdr = struct.pack(">BBBBIBBBB", ATH9K_HTC_NORMAL, 0, 0, 0, 0, ATH9K_KEY_TYPE_CLEAR,
                      ATH9K_TXKEYIX_INVALID, slot & 0xff, 0)
    return hif_htc_wrap(epid, hdr + dot11)


@dataclass
class TxQueued:
    """A sent frame in its endpoint queue after ath9k_htc_txep, waiting for its TX status."""
    cookie: int
    timestamp: float


@dataclass
class TxEvent:
    """struct ath9k_htc_tx_event: a TX status with no matching frame yet."""
    cookie: int
    epid: int
    count: int = 0


class Ath9kHtcTx:
    """priv->tx (struct ath9k_htc_tx) plus wmi->pending_tx_events."""

    def __init__(self, epids: list[int]) -> None:
        self.tx_slot = bytearray(MAX_TX_BUF_NUM)
        self.epid_queues: dict[int, list[TxQueued]] = {epid: [] for epid in epids}
        self.pending_tx_events: list[TxEvent] = []
        self.tx_lock = threading.Lock()
        self.txstatus_cnt_warned = False

    def get_slot(self) -> int:
        """ath9k_htc_tx_get_slot: take the lowest free slot, or return -ENOBUFS."""
        with self.tx_lock:
            slot = next((i for i in range(MAX_TX_BUF_NUM) if not self.tx_slot[i]), -1)
            if slot < 0:
                return -errno.ENOBUFS
            self.tx_slot[slot] = 1
            return slot

    def clear_slot(self, slot: int) -> None:
        """ath9k_htc_tx_clear_slot."""
        with self.tx_lock:
            if 0 <= slot < MAX_TX_BUF_NUM:
                self.tx_slot[slot] = 0

    def txep(self, epid: int, cookie: int) -> None:
        """ath9k_htc_txep for a sent frame: queue it on its endpoint with the time it went out."""
        with self.tx_lock:
            self.epid_queues[epid].append(TxQueued(cookie, time.monotonic()))

    def _get_packet(self, cookie: int, epid: int) -> TxQueued | None:
        """ath9k_htc_tx_get_packet; called with tx_lock held."""
        queue = self.epid_queues.get(epid)
        if queue is None:
            logger.error("Invalid EPID: %d", epid)          # get_htc_epid_queue
            return None
        for queued in queue:
            if queued.cookie == cookie:
                queue.remove(queued)
                return queued
        logger.debug("No matching packet for cookie: %d, epid: %d", cookie, epid)
        return None

    def txstatus(self, event: bytes) -> None:
        """ath9k_htc_txstatus: free the slot of each frame with a status; keep unmatched statuses."""
        cnt = event[0] if event else 0
        if cnt > HTC_MAX_TX_STATUS:
            if not self.txstatus_cnt_warned:
                self.txstatus_cnt_warned = True
                logger.warning("txstatus cnt %d > HTC_MAX_TX_STATUS", cnt)   # WARN_ON_ONCE. htc_drv_txrx.c:655
            return
        event = event.ljust(1 + 3 * cnt, b"\x00")          # the C reads inside its 64-byte REG_IN skb
        with self.tx_lock:
            for i in range(cnt):
                cookie, ts_rate = event[1 + i * 3], event[2 + i * 3]
                epid = (ts_rate & ATH9K_HTC_TXSTAT_EPID) >> ATH9K_HTC_TXSTAT_EPID_S
                if self._get_packet(cookie, epid) is None:
                    self.pending_tx_events.append(TxEvent(cookie, epid))
                    continue
                self.tx_slot[cookie] = 0                   # ath9k_htc_tx_process

    def cleanup_timer(self) -> None:
        """ath9k_htc_tx_cleanup_timer: match pending statuses, drop them after 40 ticks, and free
        the slot of any frame with no status after 3 s."""
        now = time.monotonic()
        with self.tx_lock:
            for event in list(self.pending_tx_events):
                if self._get_packet(event.cookie, event.epid) is not None:
                    logger.debug("Found packet for cookie: %d, epid: %d", event.cookie, event.epid)
                    self.pending_tx_events.remove(event)
                    self.tx_slot[event.cookie] = 0
                    continue
                event.count += 1
                if event.count >= ATH9K_HTC_TX_TIMEOUT_COUNT:
                    self.pending_tx_events.remove(event)
            for queue in self.epid_queues.values():
                for queued in list(queue):
                    if now > queued.timestamp + ATH9K_HTC_TX_TIMEOUT_INTERVAL / 1000:
                        logger.debug("Dropping a packet due to TX timeout")
                        queue.remove(queued)
                        self.tx_slot[queued.cookie] = 0

    def tx_drain(self) -> None:
        """ath9k_htc_tx_drain after htc_stop: free every queued frame's slot, drop pending statuses."""
        with self.tx_lock:
            for queue in self.epid_queues.values():
                for queued in queue:
                    self.tx_slot[queued.cookie] = 0
                queue.clear()
            self.pending_tx_events.clear()
