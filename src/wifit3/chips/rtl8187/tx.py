"""RTL8187 TX path: build tx_hdr (8187L) / rtl8187b_tx_hdr (8187B) + bulk-OUT inject.

Ported from ``rtl8187_tx`` (dev.c:227-323).

The 12-byte ``rtl8187_tx_hdr`` is prepended to the 802.11 frame:

    __le32 flags;        ← bits 0..11  = frame length (NOT incl. tx_hdr)
                          bit 15      = NO_ENC (disable HW encryption)
                          bit 17      = MOREFRAG
                          bit 18      = CTS (CTS-to-self when paired w/ RTS)
                          bit 23      = RTS
                          bits 19..22 = RTS rate
                          bits 24..27 = data rate (hw_value)
    __le16 rts_duration; ← only set when RTS or CTS bit is on
    __le16 len;          ← kernel writes 0 here (NOT the frame len; that's in flags)
    __le32 retry;        ← (rate_count - 1) << 8

Bulk-OUT endpoint = 0x02 (8187L); the 8187B picks one per frame (rtl8187b_tx_ep).

For monitor-mode injection (deauths, probes, EAPOL crafted by attacks)
we use a fixed conservative config: rate index 0 (1 Mbps CCK — the most
robust broadcast rate), NO_ENC set, no RTS, retry=RETRY_COUNT.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Optional

import usb.core
import usb.util

from .constants import (
    IEEE80211_FCTL_MOREFRAGS,
    RETRY_COUNT,
    TX_DESC_FLAG_MOREFRAG,
    TX_DESC_FLAG_NO_ENC,
    USB_EP_BULK_OUT,
)

logger = logging.getLogger(__name__)

TX_HDR_SIZE = 12
RTL8187B_TX_HDR_SIZE = 32

# rtl818x_rates[].bitrate (dev.c:84-97), in 100 kbps, indexed by hw_value.
rtl818x_rates_bitrate = (10, 20, 55, 110, 60, 90, 120, 180, 240, 360, 480, 540)

# rtl8187_tx 8187B endpoints (dev.c:288, 304-307): epmap is indexed by mac80211 queue (AC).
epmap = (6, 7, 5, 4)
RTL8187B_EP_MGMT = 12

# mac80211 wme.c:23-32, queue = AC: VO=0, VI=1, BE=2, BK=3
ieee802_1d_to_ac = (2, 3, 3, 2, 1, 1, 0, 0)

# 802.11 broadcast/management default — 1 Mbps CCK = rate hw_value 0.
RATE_1MBPS_CCK = 0
RATE_2MBPS_CCK = 1
RATE_6MBPS_OFDM = 4
RATE_24MBPS_OFDM = 8


def build_tx_hdr(
    frame_len: int,
    *,
    rate_hw_value: int = RATE_1MBPS_CCK,
    retry_count: int = RETRY_COUNT,
    morefrag: bool = False,
) -> bytes:
    """Build the 12-byte rtl8187_tx_hdr.

    Mirrors the L-branch of rtl8187_tx (dev.c:247-285).

    Args:
        frame_len: length of the 802.11 frame in bytes (max 4095).
        rate_hw_value: index into the rates table; defaults to 1 Mbps
            CCK (matches kernel default for broadcast mgmt frames).
        retry_count: how many TX attempts before giving up. Kernel
            uses RETRY_COUNT (7) by default.
        morefrag: set bit 17 if the frame is not the last fragment.
    """
    if frame_len > 0x0FFF:
        raise ValueError(f"frame too long for tx_hdr ({frame_len} > 4095)")
    if frame_len <= 0:
        raise ValueError(f"frame_len must be positive (got {frame_len})")

    flags = frame_len & 0x0FFF
    flags |= TX_DESC_FLAG_NO_ENC                # bit 15
    flags |= (rate_hw_value & 0xF) << 24        # bits 24..27
    if morefrag:
        flags |= TX_DESC_FLAG_MOREFRAG

    # Kernel writes hdr->len = 0 — the frame length goes in `flags`.
    # The struct field is just padding for alignment.
    rts_duration = 0
    length_field = 0
    retry = ((retry_count - 1) & 0xFF) << 8

    return struct.pack("<IHHI", flags, rts_duration, length_field, retry)


def ieee80211_frame_duration(length: int, rate: int, erp: int, short_preamble: bool) -> int:
    """mac80211 util.c:117-172 for the 2.4 GHz band; ``rate`` in 100 kbps, SIFS included."""
    if erp:
        dur = 16
        dur += 16
        dur += 4
        dur += 4 * -(-((16 + 8 * (length + 4) + 6) * 10) // (4 * rate))
    else:
        dur = 10
        dur += (72 + 24) if short_preamble else (144 + 48)
        dur += -(-(8 * (length + 4) * 10) // rate)
    return dur


def build_rtl8187b_tx_hdr(
    frame_len: int,
    *,
    rate_hw_value: int = RATE_1MBPS_CCK,
    retry_count: int = RETRY_COUNT,
    morefrag: bool = False,
) -> bytes:
    """Build the 32-byte rtl8187b_tx_hdr (rtl8187.h:72-82), as the 8187B branch of
    rtl8187_tx (dev.c:286-308). tx_duration is computed after skb_push, so its length
    includes this header; with no vif (monitor) erp = 0 and short_preamble = false (util.c:186)."""
    if frame_len > 0x0FFF:
        raise ValueError(f"frame too long for tx_hdr ({frame_len} > 4095)")
    if frame_len <= 0:
        raise ValueError(f"frame_len must be positive (got {frame_len})")

    flags = frame_len & 0x0FFF
    flags |= TX_DESC_FLAG_NO_ENC
    flags |= (rate_hw_value & 0xF) << 24
    if morefrag:
        flags |= TX_DESC_FLAG_MOREFRAG

    rts_duration = 0
    retry = ((retry_count - 1) & 0xFF) << 8
    tx_duration = ieee80211_frame_duration(frame_len + RTL8187B_TX_HDR_SIZE,
                                           rtl818x_rates_bitrate[rate_hw_value], 0, False)
    # flags, rts_duration, len, unused_1, unused_2, tx_duration, unused_3, retry, unused_4[2]
    return struct.pack("<IHHIHHII8x", flags, rts_duration, 0, 0, 0, tx_duration, 0, retry)


def ieee80211_select_queue_80211(frame: bytes) -> int:
    """mac80211 wme.c:117-145 for a monitor-injected frame (no ACM, no sta): the AC queue."""
    fc = frame[0]
    if (fc & 0x0C) != 0x08:                       # !ieee80211_is_data: priority 7
        return ieee802_1d_to_ac[7]
    if not (fc & 0x80):                           # !ieee80211_is_data_qos: priority 0
        return ieee802_1d_to_ac[0]
    qos_ctl = 30 if (frame[1] & 0x03) == 0x03 else 24     # ieee80211_get_qos_ctl
    return ieee802_1d_to_ac[frame[qos_ctl] & 0x07]


def rtl8187b_tx_ep(frame: bytes) -> int:
    """The 8187B bulk-OUT endpoint for ``frame`` (dev.c:304-307)."""
    if (frame[0] & 0x0C) == 0x00:                 # IEEE80211_FTYPE_MGMT
        return RTL8187B_EP_MGMT
    return epmap[ieee80211_select_queue_80211(frame)]


def stamp_seq_ctrl(frame: bytearray, seqno: int) -> int:
    """Stamp the 802.11 sequence number into seq_ctrl (bytes 22-23), preserving the
    fragment number (low 4 bits), and return the advanced seqno for the next frame.

    Mirrors the kernel L-path (``rtl8187_tx``, dev.c:270-275): advance by 0x10 (one
    sequence, since the number lives in bits [4:15]) on a *first* fragment (frag==0) and
    reuse it for later fragments of the same MSDU, so a fragment burst shares one seq.

    The 8187L has **no hardware sequence assignment** on the L-path — the HW_SEQNUM TX_CONF
    bit is 8187B-only — unlike the Ralink ``NEW_SEQ`` / rtw88 auto-seq the rest of the stack
    assumes ("hardware usually overwrites seq"). Without this, every injected frame leaves
    seq=0, and an AP dedups our multi-frame association/EAPOL conversation (PMKID extraction,
    WPS) as retransmissions — single frames (deauth) and replays (ARP, carrying a captured
    seq) are unaffected, which is why those worked and these didn't.
    """
    if len(frame) < 24:          # control frames carry no seq_ctrl — nothing to stamp
        return seqno
    frag = frame[22] & 0x0F
    if frag == 0:
        seqno = (seqno + 0x10) & 0xFFF0
    sctl = seqno | frag
    frame[22] = sctl & 0xFF       # seq_ctrl is __le16
    frame[23] = (sctl >> 8) & 0xFF
    return seqno


@dataclass(frozen=True)
class TxCloseDescriptor:
    """One 8187B "tx close descriptor" status (dev.c:483-492)."""
    pkt_rc: int         # Packet Retry Count
    tok: bool           # TOK: the frame was ACKed
    seq_no: int         # the low 12 bits of seq_ctrl, per the dev.c:513-521 note


def rtl8187b_status_cb(buf: bytes) -> Optional[TxCloseDescriptor]:
    """Decode one b_tx_status buffer from bulk-IN ep 9 (dev.c:460-545); None unless
    cmd_type is 1 (tx close descriptor). mac80211's status reporting has no wifit3 consumer."""
    if len(buf) < 8:
        return None
    val = int.from_bytes(buf[:8], "little")
    cmd_type = (val >> 30) & 0x3
    if cmd_type != 1:
        return None
    return TxCloseDescriptor(pkt_rc=val & 0xFF, tok=bool(val & (1 << 15)),
                             seq_no=(val >> 16) & 0xFFF)


def bulk_out_max_packet_size(dev: usb.core.Device, ep: int = USB_EP_BULK_OUT) -> int:
    """wMaxPacketSize of bulk-OUT ``ep``: the size URB_ZERO_PACKET (dev.c:315) pads against."""
    intf = dev.get_active_configuration()[(0, 0)]
    endpoint = usb.util.find_descriptor(intf, bEndpointAddress=ep)
    if endpoint is None:
        raise IOError(f"rtl8187: no bulk-OUT endpoint 0x{ep:02x}")
    return endpoint.wMaxPacketSize


def inject_frame(
    dev: usb.core.Device,
    frame: bytes,
    max_packet_sizes: dict[int, int],
    is_rtl8187b: bool,
    *,
    rate_hw_value: int = RATE_1MBPS_CCK,
    retry_count: int = RETRY_COUNT,
    timeout_ms: int = 1000,
) -> int:
    """Prepend the tx header and write to the frame's bulk-OUT endpoint.

    ``max_packet_sizes`` maps each bulk-OUT endpoint to its wMaxPacketSize. Returns the
    number of bytes the controller accepted. Raises USBError on transport failure.

    Default bulk-OUT timeout is 1s — unicast frames take up to
    ~140ms per send when the chip retries 7× waiting for an ACK that
    never comes (spoofed deauth pattern). Callers wanting fire-and-
    forget behaviour should pass ``retry_count=1`` and a tight
    ``timeout_ms``.
    """
    morefrag = bool(int.from_bytes(frame[:2], "little") & IEEE80211_FCTL_MOREFRAGS)  # dev.c:251
    if not is_rtl8187b:
        ep = USB_EP_BULK_OUT
        hdr = build_tx_hdr(len(frame), rate_hw_value=rate_hw_value, retry_count=retry_count,
                           morefrag=morefrag)
    else:
        ep = rtl8187b_tx_ep(frame)
        hdr = build_rtl8187b_tx_hdr(len(frame), rate_hw_value=rate_hw_value,
                                    retry_count=retry_count, morefrag=morefrag)
    payload = hdr + frame
    sent = dev.write(ep, payload, timeout_ms)
    if sent != len(payload):
        logger.warning(
            "bulk-OUT short write: sent=%d expected=%d", sent, len(payload)
        )
    # URB_ZERO_PACKET (dev.c:315): a transfer that is an exact multiple of wMaxPacketSize
    # has no terminating short packet, so the chip never fires it. PyUSB adds no ZLP.
    if sent and sent % max_packet_sizes[ep] == 0:
        dev.write(ep, b"", timeout_ms)
    return sent


# ----------------------------------------------------------------------
# Convenience: build an 802.11 deauth frame (aireplay-style).
# Useful for the M5 hardware test and as a building block for attacks.
# ----------------------------------------------------------------------
DEAUTH_REASON_CLASS3 = 7        # Class-3 frame from non-associated STA
DEAUTH_REASON_UNSPECIFIED = 1

BROADCAST_MAC = bytes.fromhex("ffffffffffff")


def build_deauth(
    target_mac: bytes,
    bssid: bytes,
    *,
    src_mac: bytes | None = None,
    reason: int = DEAUTH_REASON_CLASS3,
) -> bytes:
    """Construct a 26-byte 802.11 deauthentication frame.

    target_mac = where the deauth lands (the STA you want to bump, or
        ``BROADCAST_MAC`` for "everyone on this BSSID").
    bssid      = AP's MAC.
    src_mac    = sender. Defaults to bssid (so it looks like the AP sent
        it — the standard aireplay-ng pattern).
    """
    if len(target_mac) != 6 or len(bssid) != 6:
        raise ValueError("MAC addresses must be 6 bytes")
    if src_mac is None:
        src_mac = bssid
    if len(src_mac) != 6:
        raise ValueError("src_mac must be 6 bytes")

    # Frame Control: type=mgmt (0), subtype=deauth (12)
    #   octet 0: subtype<<4 | type<<2 | version = 0xC0
    #   octet 1: flags = 0x00
    fc = bytes([0xC0, 0x00])
    # Duration/NAV: a unicast deauth reserves the medium for the SIFS + ACK the target
    # returns (0x013A µs = SIFS + a 1 Mbps ACK, matching aireplay-ng); a group-addressed
    # (broadcast) target is never ACKed → NAV 0. The 8187L does NOT fill this in for raw
    # monitor-injected frames, so we set it here.
    nav = 0 if (target_mac[0] & 0x01) else 0x013A
    duration = struct.pack("<H", nav)
    seq_ctrl = bytes([0x00, 0x00])
    reason_bytes = struct.pack("<H", reason)

    return fc + duration + target_mac + src_mac + bssid + seq_ctrl + reason_bytes
