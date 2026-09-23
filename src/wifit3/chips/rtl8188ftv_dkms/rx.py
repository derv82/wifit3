"""RTL8188FTV DKMS RX path (bulk-IN 0x81).

Ported from ``recvbuf2recvframe`` (hal/rtl8188f/usb/usb_ops.c:363+) +
``rtl8188f_query_rx_desc_status`` (hal/rtl8188f/rtl8188f_rxdesc.c) with
the descriptor layout from include/rtl8188f_xmit.h. One bulk completion
carries an 8-byte-aligned aggregation of 24-byte-desc + drvinfo +
shift + 802.11 packets; C2H packets (``RPT_SEL``) share the endpoint.
A crc/icv error or short length drops the rest of the buffer, like the
source (``goto _exit_recvbuf2recvframe``).
"""
from __future__ import annotations

import struct

RXDESC_SIZE = 24
RX_DRV_INFO_UNIT = 8


def _bits(word: int, shift: int, width: int) -> int:
    return (word >> shift) & ((1 << width) - 1)


def decode_desc(d: bytes) -> dict:
    dw0 = struct.unpack_from("<I", d, 0)[0]
    dw2 = struct.unpack_from("<I", d, 8)[0]
    dw3 = struct.unpack_from("<I", d, 12)[0]
    return {
        "pkt_len": _bits(dw0, 0, 14),
        "crc_err": _bits(dw0, 14, 1),
        "icv_err": _bits(dw0, 15, 1),
        "drvinfo_sz": _bits(dw0, 16, 4) * RX_DRV_INFO_UNIT,
        "shift_sz": _bits(dw0, 24, 2),
        "physt": _bits(dw0, 26, 1),
        "c2h": _bits(dw2, 28, 1),
        "rate": _bits(dw3, 0, 7),
        "bssid_fit": _bits(dw3, 12, 2),
        "agg_pktnum": _bits(dw3, 16, 8),
    }


def iter_rx(buf: bytes):
    off = 0
    while off + RXDESC_SIZE <= len(buf):
        a = decode_desc(buf[off:off + RXDESC_SIZE])
        if a["crc_err"] or a["icv_err"]:
            return
        start = off + RXDESC_SIZE + a["drvinfo_sz"] + a["shift_sz"]
        if a["pkt_len"] <= 0 or start + a["pkt_len"] > len(buf):
            return
        yield a, bytes(buf[start:start + a["pkt_len"]])
        off = (start + a["pkt_len"] + 7) & ~7
