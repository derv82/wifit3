"""MT76x0U TX path — builds the bulk-OUT packet for an 802.11 frame.

**Endpoint choice**: mac80211-driven mt76 USB sends MGMT through AC_VO
(EP 0x07), not HCCA (EP 0x09). HCCA is the kernel's reserved endpoint for
beacon broadcast and power-save delivery — no normal MAC TX queue is
bound to it for user-driven inject, so bulk-OUT to EP 0x09 just sits in
the USB stack until WinUSB times out. mt76x2u (sibling driver, proven on
hw) uses AC_VO + QSEL=EDCA; we match that. The kernel's
`mt76x02u_tx_prepare_skb` only sets QSEL=MGMT when the ep == HCCA, which
in practice happens only for chip-internal beacon TX, not host inject.

[SRC] mt76x02_mac.h:135-147     (struct mt76x02_txwi)
[SRC] mt76x02_mac.c:335-437     (mt76x02_mac_write_txwi)
[SRC] mt76x02_usb_core.c:46-117 (mt76x02u_skb_dma_info + tx_prepare_skb)
[SRC] mt76x02_dma.h:12-21       (MT_TXD_INFO_* bit fields)
[SRC] dma.h:143-148             (enum mt76_qsel)

Bulk-OUT packet layout (on the wire):

  Offset  Size  Field
  0       4     DMA info word (le32):
                  - bits  0-15: INFO_LEN = round_up(TXWI + frame, 4) -- NOT
                    including the 4-byte DMA hdr itself.
                  - bit  19:    INFO_80211 (1 = 802.11 frame, not Ethernet)
                  - bit  24:    INFO_WIV  (1 = wcid invalid / no HW key)
                  - bits 25-26: QSEL (EDCA=2 for AC_VO route)
                  - bits 27-29: DPORT (WLAN_PORT=0)
  4       20    mt76x02_txwi:
                  - flags    (le16) -- AMPDU / MMPS / TS flags
                  - rate     (le16) -- 0 lets chip pick (uses rate table)
                  - ack_ctl  (u8)   -- ACK_REQ / NSEQ / BA_WIN
                  - wcid     (u8)   -- station idx; 0xFF for "no station"
                  - len_ctl  (le16) -- frame length (NOT including TXWI)
                  - iv       (le32) -- CCMP IV (0 if no encryption)
                  - eiv      (le32) -- CCMP EIV
                  - aid      (u8)
                  - txstream (u8)
                  - ctl2     (u8)   -- TX_PWR_ADJ
                  - pktid    (u8)   -- TX status correlation tag
  24      N     802.11 frame (header + body, NO FCS — chip appends it)
  ...     0-3   alignment pad to 4-byte boundary
  END     4     zero tail (per `mt76x02u_skb_dma_info` adjust_pad)

Total packet size = 4 + 20 + N + pad + 4 = round_up(N + 24, 4) + 4
"""
from __future__ import annotations

import logging
import struct

from .constants import (
    EP_OUT_AC_VO,
    MT_QSEL_EDCA,
    MT_TXD_INFO_80211,
    MT_TXD_INFO_DPORT_SHIFT,
    MT_TXD_INFO_LEN_SHIFT,
    MT_TXD_INFO_QSEL_SHIFT,
    MT_TXD_INFO_WIV,
    MT_TXWI_ACK_CTL_REQ,
    WLAN_PORT,
)
from .transport import MT76x0UTransport

logger = logging.getLogger(__name__)


# TXWI `rate` field encoding ([SRC] mt76x02_mac.h:86-92):
#   bits  0-5  : RATE_INDEX
#   bit   6    : LDPC
#   bits  7-8  : BW (0=20MHz)
#   bit   9    : SGI
#   bit  10    : STBC
#   bit  11    : LDPC_EXSYM
#   bits 13-15 : PHY (0=CCK, 1=OFDM, 2=HT-mix, 3=HT-GF, 4=VHT)
#
# A `rate=0` default means CCK PHY + IDX 0 = 1 Mbps, which the chip often
# silently DROPS for transmit. mt76x2u (sibling driver, hw-proven inject)
# defaults to OFDM PHY + IDX 0 = 6 Mbps — the lowest universal OFDM rate
# that works on both 2.4 and 5 GHz. Match that.
TXWI_RATE_OFDM_6MBPS = (1 << 13) | 0    # PHY=OFDM, idx=0


class TXError(Exception):
    """Raised when a TX descriptor can't be built or the bulk-OUT fails."""


def build_txwi(
    frame_len: int,
    *,
    request_ack: bool = False,
    wcid: int = 0xFF,
    rate: int = TXWI_RATE_OFDM_6MBPS,
) -> bytes:
    """Build a 20-byte mt76x02_txwi for a raw 802.11 frame.

    Defaults match a monitor-mode inject (no station, chip picks rate, no
    encryption, no AMPDU). Kernel paths we DO NOT exercise (and don't need
    for inject) — port them when the use case appears:
      - HW key encryption (sets iv/eiv from CCMP PN)
      - per-station tx_info (rateval / max_txpwr from wcid->tx_info)
      - AMPDU (sets BA_WINDOW field + MT_TXWI_FLAGS_AMPDU + MPDU_DENSITY)
      - MT_TXWI_FLAGS_TS for beacons / probe responses (chip stamps TSF)
      - MT_TXWI_FLAGS_MMPS / TXSTREAM for MU-MIMO (mt76x2 only)

    [SRC] mt76x02_mac.c:335-437.
    """
    if frame_len < 10 or frame_len > 4096:
        raise TXError(f"build_txwi: bad frame_len {frame_len}")
    flags = 0
    ack_ctl = MT_TXWI_ACK_CTL_REQ if request_ack else 0
    # Field layout matches struct mt76x02_txwi (packed, aligned(4)):
    #   flags(le16) rate(le16) ack_ctl(u8) wcid(u8) len_ctl(le16)
    #   iv(le32) eiv(le32) aid(u8) txstream(u8) ctl2(u8) pktid(u8)
    return struct.pack(
        "<HHBBHIIBBBB",
        flags & 0xFFFF,                  # flags
        rate  & 0xFFFF,                  # rate
        ack_ctl & 0xFF,                  # ack_ctl
        wcid & 0xFF,                     # wcid
        frame_len & 0xFFFF,              # len_ctl
        0,                               # iv
        0,                               # eiv
        0,                               # aid
        0,                               # txstream
        0,                               # ctl2 (TX_PWR_ADJ)
        0,                               # pktid
    )


def _build_dma_info(payload_len: int, *, wcid_invalid: bool = True) -> bytes:
    """Build the 4-byte DMA-info header that precedes TXWI.

    `payload_len` = len(TXWI) + len(frame), rounded up to 4-byte boundary
    per kernel `mt76x02u_skb_dma_info`. ([SRC] mt76x02_usb_core.c:46-62)

    For monitor-mode inject (no HW key), pass `wcid_invalid=True` to set
    MT_TXD_INFO_WIV.
    """
    length_field = (payload_len + 3) & ~0x3   # round up to 4

    info = (
        (length_field << MT_TXD_INFO_LEN_SHIFT)
        | MT_TXD_INFO_80211
        | (MT_TXD_INFO_WIV if wcid_invalid else 0)
        | (MT_QSEL_EDCA << MT_TXD_INFO_QSEL_SHIFT)
        | (WLAN_PORT << MT_TXD_INFO_DPORT_SHIFT)
    ) & 0xFFFFFFFF
    return struct.pack("<I", info)


def build_inject_packet(
    frame: bytes,
    *,
    request_ack: bool = False,
    wcid: int = 0xFF,
) -> bytes:
    """Assemble the full bulk-OUT packet for `frame` (an 802.11 MAC frame
    WITHOUT FCS — chip appends FCS). Returns the bytes to push out EP 0x09.

    Kernel `mt76x02u_tx_prepare_skb` also inserts a 2-byte header pad when
    the 802.11 header length isn't 4-byte aligned (`mt76_insert_hdr_pad`).
    We DO NOT do that here — for the standard mgmt frames we inject
    (deauth, disassoc, probe-req, ...) the headers are 24 bytes (already
    4-aligned). When QoS-data or 4-addr WDS shows up we'll need to port
    the pad insert.
    """
    txwi = build_txwi(len(frame), request_ack=request_ack, wcid=wcid)
    payload = txwi + frame
    dma_info = _build_dma_info(len(payload))

    # Pad payload (TXWI + frame) up to 4-byte boundary, then append a 4-byte
    # zero tail (kernel adjust_pad does this in one go).
    pad_to_align = ((-len(payload)) & 0x3)
    zero_tail = 4

    return dma_info + payload + b"\x00" * (pad_to_align + zero_tail)


def inject_80211_frame(
    transport: MT76x0UTransport,
    frame: bytes,
    *,
    request_ack: bool = False,
    wcid: int = 0xFF,
    timeout_ms: int = 1000,
) -> int:
    """Send `frame` via EP 0x09 (HCCA = MGMT queue). Returns the number
    of bytes pushed (full packet length).

    Raises TXError on USB failure.
    """
    pkt = build_inject_packet(frame, request_ack=request_ack, wcid=wcid)
    logger.trace("inject_80211_frame: %d-byte 802.11 frame -> %d-byte bulk-OUT",
                 len(frame), len(pkt))
    try:
        transport.bulk_out(EP_OUT_AC_VO, pkt, timeout_ms=timeout_ms)
    except Exception as e:
        raise TXError(f"bulk-OUT to EP 0x{EP_OUT_AC_VO:02x} failed: {e}") from e
    return len(pkt)


def stamp_seq_ctrl(frame: bytearray, seqno: int) -> int:
    """Stamp an incrementing 802.11 sequence number into seq_ctrl (bytes 22-23),
    preserving the fragment number (low 4 bits); return the advanced seqno.

    build_txwi never sets NSEQ, so the mt76x02 chip transmits the seq_ctrl already in the
    MPDU. build_deauth_frame leaves it 0, so without this every inject shares seq 0: an AP
    dedups a multi-frame conversation as retransmissions, and a retransmit histogram folds
    a whole run into one bucket (bench-confirmed 2026-07-16 on the MT7610U). The number
    lives in bits [4:15], so one step is 0x10; a fragment burst (frag>0) reuses one number.
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


# ---------------------------------------------------------------------------
# Frame builders for common attacks. Kept here (vs in attacks/) so the TX
# path's reference frames are colocated with the codec.
# ---------------------------------------------------------------------------


def build_deauth_frame(
    dst: bytes, src: bytes, bssid: bytes, *, reason: int = 7,
) -> bytes:
    """Build a 26-byte 802.11 deauth frame.

    Per IEEE 802.11-2020 §9.3.3.13: type=MGMT, subtype=DEAUTH (0x0C):
      FC[0] = 0xC0      (subtype 12 << 4 | type MGMT << 2)
      FC[1] = 0x00      (no flags — not retry, not from-DS / to-DS)
      Duration (2B)     -- 0 (chip can update)
      Address 1 (6B)    -- destination MAC
      Address 2 (6B)    -- source MAC (spoofed = BSSID for AP→client deauth)
      Address 3 (6B)    -- BSSID
      Seq Ctl (2B)      -- 0 (chip fills if NSEQ)
      Reason Code (2B)  -- e.g. 7 = "Class 3 frame received from nonassociated STA"

    Default reason 7 matches `aireplay-ng --deauth`. Reason 2 = "previous
    auth no longer valid" is also common.

    For [[no-ssids-in-commits]] — `dst/src/bssid` come from caller-supplied
    bytes; this function doesn't log them.
    """
    if len(dst) != 6 or len(src) != 6 or len(bssid) != 6:
        raise TXError("build_deauth_frame: addresses must be 6 bytes each")
    return (
        b"\xC0\x00"                          # FC: subtype=deauth, type=mgmt
        + b"\x3A\x01"                        # duration = 0x013A us (matches mt76x2u)
        + dst + src + bssid                  # 3 addrs
        + b"\x00\x00"                        # seq ctl
        + struct.pack("<H", reason & 0xFFFF) # reason code
    )


def build_disassoc_frame(
    dst: bytes, src: bytes, bssid: bytes, *, reason: int = 7,
) -> bytes:
    """Disassoc = MGMT subtype 10. Identical layout to deauth. [SRC] §9.3.3.5"""
    if len(dst) != 6 or len(src) != 6 or len(bssid) != 6:
        raise TXError("build_disassoc_frame: addresses must be 6 bytes each")
    return (
        b"\xA0\x00"
        + b"\x3A\x01"                        # duration
        + dst + src + bssid
        + b"\x00\x00"
        + struct.pack("<H", reason & 0xFFFF)
    )
