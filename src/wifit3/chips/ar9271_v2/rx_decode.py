"""Bulk-IN RX frame decode — the ath9k_htc HIF RX stream into 802.11 frames.

The WLAN_RX bulk pipe (EP 0x82) carries one or more HTC RX frames per USB transfer, each
wrapped by a HIF stream header. This is the device->host *frame* decode, which the host-side
pcap gate does NOT model (it verifies host->device ops only) — so it is validated offline against
the capture's recorded RX frames and on live hardware, not by the gate.

Per-frame wire layout [SRC] hif_usb.c:592-668 + htc_drv_txrx.c:988-1034 + mac.h:155-176:

    [__le16 pkt_len][__le16 tag=0x4e00]      HIF stream header (4 B)
    [htc_frame_hdr            8 B]            endpoint_id, flags, be16 payload_len, control[4]
    [ath_htc_rx_status       40 B]           be64 tstamp, be16 rs_datalen, rs_status, rs_rssi, ...
    [802.11 frame, rs_datalen B]             includes the trailing 4-byte FCS
    [pad to the next 4-byte boundary]

``pkt_len`` counts the htc_frame_hdr + rx_status + 802.11 frame (8 + 40 + rs_datalen). The HTC
layer strips the 8-byte htc_frame_hdr before ath9k_rx_prepare, which then sees rx_status + frame
and checks ``rs_datalen == skb->len - 40`` [SRC] htc_drv_txrx.c:996. The next frame starts at
``index + 4 + pkt_len + pad``, pad = (4 - (pkt_len & 3)) & 3 [SRC] hif_usb.c:620-625.
"""
from __future__ import annotations

import struct
from typing import Iterator, Tuple

HIF_RX_STREAM_TAG = 0x4E00           # [SRC] hif_usb.h:50 ATH_USB_RX_STREAM_MODE_TAG
HTC_FRAME_HDR_LEN = 8                 # [SRC] htc_hst.h:59-64
RX_STATUS_LEN = 40                    # [SRC] htc.h:272 HTC_RX_FRAME_HEADER_SIZE
FCS_LEN = 4                           # the 802.11 trailer the driver strips before the callback

# rs_status error bits [SRC] mac.h:178-184 — frames the hardware flagged as undecodable.
RXERR_CRC = 0x01
RXERR_PHY = 0x02
RXERR_FIFO = 0x04
RXERR_CORRUPT_DESC = 0x40
# Drop on any of these before the parser: ath9k_rx_prepare bails on PHY errors and
# ath9k_cmn_rx_accept rejects CRC/corrupt frames [SRC] htc_drv_txrx.c:1005 / common.c:77.
_DROP_STATUS = RXERR_CRC | RXERR_PHY | RXERR_FIFO | RXERR_CORRUPT_DESC

# Offsets within ath_htc_rx_status [SRC] mac.h:155-176.
_OFF_DATALEN = 8                      # be16 rs_datalen, after the be64 rs_tstamp
_OFF_STATUS = 10                      # u8 rs_status
_OFF_RSSI = 12                        # int8_t rs_rssi (dBm, signed)

ATH9K_RXKEYIX_INVALID = 0xFF         # [SRC] mac.h:194
_ATH_KEYMAX = 128                     # [SRC] common.h ATH_KEYMAX
_OFF_KEYIX = 19                       # u8 rs_keyix

# rs_rssi is the Atheros RSSI in dB *above the noise floor* (a positive SNR-like value), not
# absolute dBm. The kernel folds in the channel noise floor: signal = ah->noise + rs_rssi
# [SRC] common.c:272. We use the nominal default NF (no live per-channel NF tracking here); the
# result is a sane negative dBm for the AP table / RX-health bar [[beacon_rate_bar]].
_DEFAULT_NF_DBM = -95                 # [SRC] hw.h:72 ATH_DEFAULT_NOISE_FLOOR


def iter_frames(buf: bytes) -> Iterator[Tuple[bytes, int]]:
    """Yield ``(mpdu, rssi_dbm)`` for each good 802.11 frame in one bulk-IN transfer.

    The trailing FCS is stripped so ``len(mpdu)`` is the MPDU body — the wifit3 driver contract
    [[project_rx_frames_include_fcs]]. Frames the hardware flagged CRC/PHY/FIFO/corrupt are
    dropped; a bad stream tag or a frame truncated by the transfer boundary ends the buffer
    (cross-transfer reassembly is not modelled — a split frame is lost, not corrupted)."""
    i, n = 0, len(buf)
    while i + 4 <= n:
        pkt_len, tag = struct.unpack_from("<HH", buf, i)
        if tag != HIF_RX_STREAM_TAG:
            return                                    # invalid tag -> whole buffer suspect
        start = i + 4
        end = start + pkt_len
        if end > n:
            return                                    # truncated final frame (not reassembled)
        body = buf[start:end]
        i = end + ((4 - (pkt_len & 3)) & 3)
        if pkt_len < HTC_FRAME_HDR_LEN + RX_STATUS_LEN:
            continue
        rxs = body[HTC_FRAME_HDR_LEN:HTC_FRAME_HDR_LEN + RX_STATUS_LEN]
        dot11 = body[HTC_FRAME_HDR_LEN + RX_STATUS_LEN:]
        rs_datalen = struct.unpack_from(">H", rxs, _OFF_DATALEN)[0]
        rs_status = rxs[_OFF_STATUS]
        rs_keyix = rxs[_OFF_KEYIX]
        (rssi,) = struct.unpack_from("b", rxs, _OFF_RSSI)
        if rs_status & _DROP_STATUS:
            continue
        if rs_datalen != len(dot11) or rs_datalen < 10:   # [SRC] htc_drv_txrx.c:1010 (< ACK)
            continue
        if rs_keyix >= _ATH_KEYMAX and rs_keyix != ATH9K_RXKEYIX_INVALID:
            continue                                  # [SRC] htc_drv_txrx.c:1017
        signal = (_DEFAULT_NF_DBM + rssi) if 0 < rssi < 128 else _DEFAULT_NF_DBM
        yield dot11[:rs_datalen - FCS_LEN], signal
