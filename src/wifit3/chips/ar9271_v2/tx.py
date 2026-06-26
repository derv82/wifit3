"""HTC TX framing for the AR9271 (ath9k_htc) — the wire format for injected frames.

A TX frame on the WLAN_TX bulk pipe (EP 0x01) is, outermost first:

    [HIF stream hdr]   __le16 pkt_len, __le16 tag (0x697e)            hif_usb.c (stream mode)
    [htc_frame_hdr]    u8 endpoint_id, u8 flags, __be16 payload_len, u8 control[4]   htc_hst.h
    [tx_mgmt_hdr | tx_frame_hdr]                                      htc.h:73 / :85
    [802.11 frame]

``pkt_len`` counts everything after the HIF header; ``payload_len`` counts the tx hdr + 802.11.
Management frames (probe/auth/deauth — the aireplay-ng ``--test`` + deauth phases) ride the mgmt
service endpoint with ``tx_mgmt_hdr`` (8 B); QoS/data frames ride the data endpoint with
``tx_frame_hdr`` (12 B). [SRC] htc_drv_txrx.c:222 (mgmt) / :269 (data).

Decoded against the capture but NOT yet driven — the build path is the inject_frame milestone
(``driver.inject_frame``). This module is the format reference + the harness-side extractor.
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
# 12 B). The htc_frame_hdr's endpoint_id (byte 0) selects the header layout.
TX_MGMT_EPID = 5
TX_DATA_EPID = 6
TX_MGMT_HDR_LEN = 8                 # [SRC] htc.h:85 struct tx_mgmt_hdr
TX_FRAME_HDR_LEN = 12               # [SRC] htc.h:73 struct tx_frame_hdr


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


# TODO (inject_frame milestone): build_mgmt_tx / build_data_tx — fill tx_mgmt_hdr (node_idx,
# vif_idx, tidno, flags, key_type, keyix=0xff, cookie, pad) / tx_frame_hdr, allocate the per-frame
# slot/cookie, route to the mgmt vs data endpoint, then wrap with hif_htc_wrap. The 802.11 frame
# (incl. its own sequence number) comes straight from the caller. [SRC] htc_drv_txrx.c:222 / :269.
