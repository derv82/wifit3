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

# TXWI rate field (16-bit `rateval`). [SRC] mt76x02_mac.c:218-220 composes it as
# FIELD_PREP(MT_RXWI_RATE_INDEX, idx) | FIELD_PREP(MT_RXWI_RATE_PHY, phy), with
# MT_RXWI_RATE_PHY = GENMASK(15,13) [SRC] mt76x02_mac.h:92 and MT_PHY_TYPE_CCK=0 /
# MT_PHY_TYPE_OFDM=1 [SRC] mt76.h:327.
#
# 2.4 GHz: CCK 1 Mbps (PHY=CCK, idx=0 → 0x0000) — verified on the wire in
# driver_captures/captures_mt76x2u/capture-1.pcap (frame 32207, aireplay-ng's first
# deauth bulk-OUT). 1 Mbps CCK is universally a basic rate so the AP accepts it.
_TXWI_RATE_CCK_1MBPS = 0x0000
# 5 GHz: OFDM 6 Mbps (OFDM_RATE(0,60) → hw_value=(OFDM<<8)|0 → phy=1,idx=0 →
# 1<<13 = 0x2000) [SRC] mt76.h:1172. CCK is a 2.4 GHz-only modulation: a CCK rate
# on a 5 GHz channel is invalid and the chip drops the frame, so a 5 GHz inject
# MUST go out as OFDM. The AP can still reject ToDS DATA at OFDM 6 (silently
# killing ARP-replay / ChopChop), but on 5 GHz there is no CCK fallback to pick.
_TXWI_RATE_OFDM_6MBPS = 0x2000

# 5 GHz starts at channel 36 (UNII-1); anything below is 2.4 GHz.
_CH_5GHZ_MIN = 36

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


def _txwi_rate_for_channel(channel: int) -> int:
    """TXWI rate word for an inject on `channel`: OFDM 6 Mbps on 5 GHz, CCK 1 Mbps
    on 2.4 GHz. CCK does not exist on 5 GHz, so a 5 GHz inject at CCK is dropped by
    the chip — picking by band is mandatory, not a tuning preference."""
    return _TXWI_RATE_OFDM_6MBPS if channel >= _CH_5GHZ_MIN else _TXWI_RATE_CCK_1MBPS


def assemble_tx_frame(frame_802_11: bytes, ack: bool = False,
                      rate: int = _TXWI_RATE_CCK_1MBPS) -> bytes:
    """Build the full bulk-OUT bytes for one TX inject.

    `rate` is the 16-bit TXWI rate word — CCK 1 Mbps by default (valid on 2.4 GHz);
    pass an OFDM rate for 5 GHz. Returns:
    [TXINFO 4B] + [TXWI 20B] + [hdr-pad 0 or 2B] + [802.11] + [tail-pad].
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

    txwi = build_txwi(frame_len, ack=ack, rate=rate)
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


def stamp_seq_ctrl(frame: bytearray, seqno: int) -> int:
    """Stamp an incrementing 802.11 sequence number into seq_ctrl (bytes 22-23),
    preserving the fragment number (low 4 bits); return the advanced seqno.

    The mt76x02 chip transmits the seq_ctrl already present in the MPDU: the 20-byte
    txwi carries no sequence field and build_txwi sets no NSEQ bit, so bytes 22-23 go
    on the wire verbatim (bench-confirmed 2026-07-16: injects left at seq 0 all arrived
    as seq 0, folding a whole retransmit run into one sequence number). Without this
    every inject reuses seq 0 and an AP dedups a multi-frame conversation as
    retransmissions. The number lives in bits [4:15], so one step is 0x10; a fragment
    burst (frag>0) reuses one sequence number.
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


async def inject_frame(transport: MT76x2UTransport, frame_802_11: bytes,
                       ack: bool = False, channel: int = 0) -> bool:
    """Send a raw 802.11 frame via bulk-OUT EP 0x07 (AC_VO).

    `channel` is the currently-tuned channel; it selects the TXWI rate band (OFDM
    on 5 GHz, CCK on 2.4 GHz). 0 (the default) keeps the 2.4 GHz / CCK behaviour.
    """
    blob = assemble_tx_frame(frame_802_11, ack=ack,
                             rate=_txwi_rate_for_channel(channel))
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
