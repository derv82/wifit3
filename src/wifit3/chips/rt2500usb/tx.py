"""rt2500usb TX path: 802.11 frame → TXD + bulk-OUT.

Port of rt2500usb_write_tx_desc (rt2500usb.c:1056-1111) + the rt2x00usb
``get_tx_data_len`` length rule (rt2500usb.c:1199-1211). The 5×u32 TX
descriptor is prepended to the frame; the whole buffer goes out on the
bulk-OUT endpoint (EP 0x01).

Rate: **1 Mbps CCK, long preamble** — the universal management-frame
injection rate (deauth/probe). The PLCP fields are computed the way the
rt2x00 layer does for CCK and verified byte-for-byte against the
capture's aireplay deauth TXD (usb_dumps/captures_rt2500usb/capture-1
frame 9895):

    word0 = 0x001a10f0  count=26 retry=15 ACK=0 NEW_SEQ=1 IFS=0 CCK
    word1 = 0x0000a580  AIFS=2 CWMIN=5 CWMAX=10
    word2 = 0x00f00400  signal=0x00(1Mbps) service=0x04 length=240=(26+4)*8

The chip appends the 4-byte FCS, so ``frame`` must NOT include it; the
PLCP length accounts for it (+4). Sequence numbers are assigned by the
chip (TXD_W0_NEW_SEQ=1 + TXRX_CSR1_AUTO_SEQUENCE set at init).

TX is only ever invoked behind explicit user action [[passive_by_default]].
"""
from __future__ import annotations

import logging
import struct

import usb.core

from .constants import (
    TXD_W0_ACK,
    TXD_W0_DATABYTE_COUNT,
    TXD_W0_IFS,
    TXD_W0_NEW_SEQ,
    TXD_W0_RETRY_LIMIT,
    TXD_W1_AIFS,
    TXD_W1_CWMIN,
    TXD_W1_CWMAX,
    TXD_W2_PLCP_LENGTH_HIGH,
    TXD_W2_PLCP_LENGTH_LOW,
    TXD_W2_PLCP_SERVICE,
    TXD_W2_PLCP_SIGNAL,
)
from .transport import set_field16 as set_field   # width-agnostic

logger = logging.getLogger(__name__)

# CCK 1 Mbps PLCP constants (verified vs capture deauth TXD).
_PLCP_SIGNAL_1MBPS = 0x00
_PLCP_SERVICE = 0x04
# word1 contention params, observed constant on the wire.
_AIFS = 2
_CWMIN = 5
_CWMAX = 10
_RETRY_LIMIT = 15

_USB_MAXPACKET_FS = 64          # RT2570 is full-speed; bulk maxpacket 64.
_TX_TIMEOUT_MS = 200


def build_tx_desc(frame_len: int, *, ack: bool = False) -> bytes:
    """Build the 5×u32 LE TX descriptor for a 1 Mbps CCK frame of
    ``frame_len`` bytes (excluding the FCS the chip appends)."""
    plcp_len = (frame_len + 4) * 8      # +4 FCS, ×8 µs/byte at 1 Mbps

    word0 = 0
    word0 = set_field(word0, TXD_W0_RETRY_LIMIT, _RETRY_LIMIT)
    word0 = set_field(word0, TXD_W0_ACK, 1 if ack else 0)
    word0 = set_field(word0, TXD_W0_NEW_SEQ, 1)     # chip assigns sequence
    word0 = set_field(word0, TXD_W0_IFS, 0)
    word0 = set_field(word0, TXD_W0_DATABYTE_COUNT, frame_len)

    word1 = 0
    word1 = set_field(word1, TXD_W1_AIFS, _AIFS)
    word1 = set_field(word1, TXD_W1_CWMIN, _CWMIN)
    word1 = set_field(word1, TXD_W1_CWMAX, _CWMAX)

    word2 = 0
    word2 = set_field(word2, TXD_W2_PLCP_SIGNAL, _PLCP_SIGNAL_1MBPS)
    word2 = set_field(word2, TXD_W2_PLCP_SERVICE, _PLCP_SERVICE)
    word2 = set_field(word2, TXD_W2_PLCP_LENGTH_LOW, plcp_len & 0xFF)
    word2 = set_field(word2, TXD_W2_PLCP_LENGTH_HIGH, (plcp_len >> 8) & 0xFF)

    return struct.pack("<5I", word0, word1, word2, 0, 0)


def _tx_data_len(buf_len: int, usb_maxpacket: int = _USB_MAXPACKET_FS) -> int:
    """rt2500usb_get_tx_data_len: even length, and never an exact multiple
    of the USB packet size (would need a ZLP the chip doesn't expect)."""
    length = (buf_len + 1) & ~1                 # roundup to 2
    if length % usb_maxpacket == 0:
        length += 2
    return length


def build_tx_urb(frame: bytes, *, ack: bool = False,
                 usb_maxpacket: int = _USB_MAXPACKET_FS) -> bytes:
    """TXD + frame, zero-padded to the length rt2x00usb would send."""
    buf = build_tx_desc(len(frame), ack=ack) + frame
    length = _tx_data_len(len(buf), usb_maxpacket)
    if length > len(buf):
        buf = buf + b"\x00" * (length - len(buf))
    return buf


def inject(dev: usb.core.Device, ep_out: int, frame: bytes,
           ack: bool = False, *, usb_maxpacket: int = _USB_MAXPACKET_FS) -> int:
    """Send one raw 802.11 frame (no FCS) out the bulk-OUT endpoint.
    Returns the number of bytes written.

    ``ack`` is positional-or-keyword so the driver can forward it through
    ``loop.run_in_executor`` (which only passes positional args)."""
    urb = build_tx_urb(frame, ack=ack, usb_maxpacket=usb_maxpacket)
    return dev.write(ep_out, urb, _TX_TIMEOUT_MS)
