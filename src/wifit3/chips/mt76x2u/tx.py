"""MT76x2U TX inject path.

SPDX-License-Identifier: GPL-2.0-or-later
Ported from Linux mt76 (kernel v6.18) by wifit3, 2026.

Mirrors:
  - mt76x02_mac.c::mt76x02_mac_write_txwi (20-byte mt76x02_txwi build)
  - mt76x02_usb_core.c::mt76x02u_skb_dma_info (4-byte TXINFO prefix + 4B tail pad)
  - mt76x02_usb_core.c::mt76x02u_tx_prepare_skb (full assembly + QSEL)

Wire format on bulk-OUT (MGMT goes on AC_VO = EP 0x07):

    [4B  TXINFO         ]   LEN | TYPE_80211 | WIV | QSEL=EDCA | DPORT=WLAN_PORT
    [20B mt76x02_txwi   ]   flags, rate, ack_ctl, wcid, len_ctl, iv, eiv, ...
    [2B  hdr-pad (opt)  ]   inserted if 802.11 hdr is not 4-byte aligned
    [N   802.11 frame   ]
    [pad to 4B align +4 ]   trailing zero bytes
"""
from __future__ import annotations

import logging
import struct

from .constants import EP_OUT_AC_VO
from .transport import MT76x2UTransport

logger = logging.getLogger(__name__)

# TXINFO bitfields. [SRC] mt76x02_dma.h:12
_TXD_INFO_LEN_MASK   = 0xFFFF
_TXD_INFO_80211      = 1 << 19
_TXD_INFO_WIV        = 1 << 24
_TXD_INFO_QSEL_SHIFT = 25      # bits 26:25
_TXD_INFO_DPORT_SHIFT = 27     # bits 29:27

_QSEL_MGMT = 0
_QSEL_EDCA = 2
_WLAN_PORT = 0                 # enum dma_msg_port = 0

_TXWI_LEN = 20

# Rate field. Kernel mt76x2u sends every injected mgmt frame with
# `rate=0x0000` (PHY=CCK, idx=0 = 1 Mbps) — verified on the wire in
# usb_dumps/captures_mt76x2u/capture-1.pcap (frame 32207, aireplay-ng's
# first deauth bulk-OUT). 1 Mbps CCK is universally a basic rate so the
# AP accepts it; OFDM 6 Mbps works for mgmt round-trips but the AP can
# reject ToDS DATA at that rate, silently killing ARP-replay / ChopChop.
_TXWI_RATE_CCK_1MBPS = 0x0000

# txstream: kernel sets 0x13 for 2x2 MIMO chips at rev >= E4 (the
# AWUS036ACM is E4 — capture-1 frame 32207 shows txstream=0x13 on the
# wire). [SRC] mt76x02_mac.c:397. Without this the chip drops Protected
# DATA frames even though mgmt still goes out.
_TXWI_TXSTREAM_2X2_E4 = 0x13

# TXWI flags / ack_ctl. [SRC] mt76x02_mac.h:118
_TXWI_ACK_CTL_REQ = 1 << 0


def build_txwi(frame_len: int, ack: bool = False,
               rate: int = _TXWI_RATE_CCK_1MBPS) -> bytes:
    """Build a minimal 20-byte mt76x02_txwi for an unencrypted MGMT frame.

    `frame_len` = length of the 802.11 frame body in bytes (no FCS).
    `ack` = whether the chip should request ACK + retry. For broadcast
    deauths use False; for unicast targeted deauth set True.
    """
    flags = 0
    ack_ctl = _TXWI_ACK_CTL_REQ if ack else 0
    wcid = 0xFF        # no-station (broadcast / monitor)
    aid = 0
    txstream = _TXWI_TXSTREAM_2X2_E4
    ctl2 = 0
    # pktid stays 0 (MT_PACKET_ID_NO_ACK) for every inject frame: we always
    # send with wcid=0xff, and the kernel returns MT_PACKET_ID_NO_ACK for a
    # wcid-less frame regardless of the ACK request, so the chip never
    # pushes a per-frame MT_TX_STAT_FIFO report that nothing here drains.
    # Confirmed on the wire (capture-1 frame 32207, pktid=0x00).
    # [SRC] tx.c:132-133.
    pktid = 0
    iv = 0
    eiv = 0
    return struct.pack(
        "<HH BB H II BBBB",
        flags,
        rate & 0xFFFF,
        ack_ctl,
        wcid,
        frame_len & 0xFFFF,
        iv,
        eiv,
        aid,
        txstream,
        ctl2,
        pktid,
    )


def _txinfo_word(payload_len_rounded: int, ack: bool) -> int:
    """Build the 4-byte TXINFO prefix word.

    `payload_len_rounded` = `round_up(TXWI + hdr_pad + 802.11, 4)`.
    [SRC] mt76x02_usb_core.c:46 (mt76x02u_skb_dma_info).
    """
    # MGMT on USB → kernel uses QSEL_EDCA (see mt76x02u_tx_prepare_skb:97).
    # MGMT on HCCA endpoint → QSEL_MGMT. We send on AC_VO, so EDCA.
    info = (
        (payload_len_rounded & _TXD_INFO_LEN_MASK)
        | (_WLAN_PORT << _TXD_INFO_DPORT_SHIFT)
        | (_QSEL_EDCA << _TXD_INFO_QSEL_SHIFT)
        | _TXD_INFO_80211
        | _TXD_INFO_WIV     # no hardware key — sw_iv path
    )
    return info


def assemble_tx_frame(frame_802_11: bytes, ack: bool = False) -> bytes:
    """Build the full bulk-OUT bytes for one TX inject.

    Returns: [TXINFO 4B] + [TXWI 20B] + [hdr-pad 0 or 2B] + [802.11] + [tail-pad].
    """
    frame_len = len(frame_802_11)
    # mt76_insert_hdr_pad inserts 2 bytes between MAC hdr and body when the
    # 802.11 hdr length isn't 4-byte aligned (the kernel calls it on every
    # SKB before write_txwi). For a 24-byte MGMT header it's a no-op.
    hdr_pad = 2 if (len(frame_802_11) >= 2 and _ieee80211_hdrlen(
        frame_802_11[0], frame_802_11[1]) % 4 != 0) else 0
    if hdr_pad:
        hdrlen = _ieee80211_hdrlen(frame_802_11[0], frame_802_11[1])
        frame_with_pad = (
            frame_802_11[:hdrlen] + b"\x00\x00" + frame_802_11[hdrlen:]
        )
    else:
        frame_with_pad = frame_802_11

    txwi = build_txwi(frame_len, ack=ack)
    skb_body = txwi + frame_with_pad
    rounded = (len(skb_body) + 3) & ~3
    if rounded > len(skb_body):
        skb_body = skb_body + b"\x00" * (rounded - len(skb_body))
    txinfo = struct.pack("<I", _txinfo_word(rounded, ack))
    # Tail pad: 4 trailing zero bytes per mt76_skb_adjust_pad.
    return txinfo + skb_body + b"\x00\x00\x00\x00"


def _ieee80211_hdrlen(fc0: int, fc1: int) -> int:
    ftype = (fc0 & 0x0C) >> 2
    subtype = (fc0 & 0xF0) >> 4
    if ftype == 1:
        return 10
    base = 24
    if (fc1 & 0x03) == 0x03:
        base = 30
    if ftype == 2 and (subtype & 0x08):
        base += 2
    return base


# ---------------------------------------------------------------------------
# 802.11 frame builders (driver-agnostic — could live in wlan/ but kept
# inline so the file is self-contained per chip).
# ---------------------------------------------------------------------------
def build_deauth(target_mac: bytes, bssid: bytes,
                 reason: int = 7,            # CLASS3_FROM_NONASSOC
                 from_ap: bool = True) -> bytes:
    """Build a single 26-byte 802.11 deauth frame.

    For the standard "kick client" attack, `from_ap=True` sends the deauth
    AS the AP (src/bssid = AP MAC, dst = client MAC).
    """
    if len(target_mac) != 6 or len(bssid) != 6:
        raise ValueError("target_mac and bssid must be 6 bytes each")
    fc0 = 0xC0   # type=MGMT, subtype=DEAUTH (12 << 4) = 0xC0
    fc1 = 0x00
    duration = 0x013A
    if from_ap:
        addr1 = target_mac    # DA = client
        addr2 = bssid         # SA = AP
        addr3 = bssid         # BSSID = AP
    else:
        addr1 = bssid
        addr2 = target_mac
        addr3 = bssid
    seq = 0
    return struct.pack(
        "<BB H 6s 6s 6s H H",
        fc0, fc1, duration,
        addr1, addr2, addr3,
        seq, reason,
    )


async def inject_frame(transport: MT76x2UTransport, frame_802_11: bytes,
                       ack: bool = False) -> bool:
    """Send a raw 802.11 frame via bulk-OUT EP 0x07 (AC_VO)."""
    blob = assemble_tx_frame(frame_802_11, ack=ack)
    try:
        written = await transport.async_write_bulk(
            EP_OUT_AC_VO, blob, timeout_ms=500,
        )
    except Exception as e:
        logger.error("TX inject failed: %s", e)
        return False
    if written != len(blob):
        logger.error("TX inject short write %d/%d", written, len(blob))
        return False
    logger.debug("TX inject sent %d bytes on EP 0x%02x", written, EP_OUT_AC_VO)
    return True
