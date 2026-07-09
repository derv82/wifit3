"""RTL8187L TX path: build tx_hdr + bulk-OUT inject.

Ported from ``rtl8187_tx`` (dev.c:227-) — L-branch.

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

Bulk-OUT endpoint = 0x02.

For monitor-mode injection (deauths, probes, EAPOL crafted by attacks)
we use a fixed conservative config: rate index 0 (1 Mbps CCK — the most
robust broadcast rate), NO_ENC set, no RTS, retry=RETRY_COUNT.
"""
from __future__ import annotations

import logging
import struct

import usb.core

from .constants import (
    RETRY_COUNT,
    TX_DESC_FLAG_NO_ENC,
    USB_EP_BULK_OUT,
)

logger = logging.getLogger(__name__)

TX_HDR_SIZE = 12

# High-speed bulk-OUT max packet size (EP 0x02 wMaxPacketSize on the AWUS036H). The
# kernel sets URB_ZERO_PACKET on every TX URB (dev.c:315); libusb only *appends* the
# terminating zero-length packet when the transfer is an exact multiple of this — so a
# frame whose total length is a 512-multiple would otherwise never signal end-of-transfer
# and the chip would stall instead of transmitting. PyUSB's dev.write does NOT do this, so
# we replicate it explicitly.
USB_BULK_MAXPACKET = 512

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
        flags |= 1 << 17

    # Kernel writes hdr->len = 0 — the frame length goes in `flags`.
    # The struct field is just padding for alignment.
    rts_duration = 0
    length_field = 0
    retry = ((retry_count - 1) & 0xFF) << 8

    return struct.pack("<IHHI", flags, rts_duration, length_field, retry)


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


def inject_frame(
    dev: usb.core.Device,
    frame: bytes,
    *,
    ep: int = USB_EP_BULK_OUT,
    rate_hw_value: int = RATE_1MBPS_CCK,
    retry_count: int = RETRY_COUNT,
    timeout_ms: int = 1000,
) -> int:
    """Prepend tx_hdr and write to bulk-OUT.

    Returns the number of bytes the controller accepted (should equal
    ``TX_HDR_SIZE + len(frame)``). Raises USBError on transport failure.

    Default bulk-OUT timeout is 1s — unicast frames take up to
    ~140ms per send when the chip retries 7× waiting for an ACK that
    never comes (spoofed deauth pattern). Callers wanting fire-and-
    forget behaviour should pass ``retry_count=1`` and a tight
    ``timeout_ms``.
    """
    hdr = build_tx_hdr(
        len(frame),
        rate_hw_value=rate_hw_value,
        retry_count=retry_count,
    )
    payload = hdr + frame
    sent = dev.write(ep, payload, timeout_ms)
    if sent != len(payload):
        logger.warning(
            "bulk-OUT short write: sent=%d expected=%d", sent, len(payload)
        )
    # URB_ZERO_PACKET (dev.c:315): if the transfer is an exact multiple of the bulk
    # max-packet size, the trailing short packet that signals end-of-transfer is absent,
    # so the chip waits for more data and never fires the frame. Send an explicit ZLP.
    if sent and sent % USB_BULK_MAXPACKET == 0:
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
