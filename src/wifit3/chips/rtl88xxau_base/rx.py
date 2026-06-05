"""RTL88xxAU RX path — bulk-IN buffer decode (the 24-byte RX-status descriptor walk).

Mirrors ``rtl8812_query_rx_desc_status`` [SRC] rtl8812a_rxdesc.c + ``recvbuf2recvframe``
[SRC] usb/usb_ops_linux.c:104. One bulk-IN transfer carries several USB-aggregated RX
packets, each:

    [ 24 B RX status desc ][ drvinfo_sz PHY-status ][ shift_sz pad ][ pkt_len MPDU ]

rounded up to an 8-byte boundary. The MPDU's trailing 4-byte FCS is HW-appended
(monitor RCR sets RCR_APPFCS) and stripped here before the frame is yielded — every
wifit3 driver delivers FCS-stripped frames so the parser can trust frame_end==MPDU_end.

The RX-status-desc layout is family-shared (8821au/8812au); the PHY-status RSSI formula
is per-chip and injected as ``decode_rssi(phy_status, data_rate) -> dbm``.
"""
from __future__ import annotations

from typing import Callable, Iterator, NamedTuple, Tuple

RXDESC_SIZE = 24            # [SRC] rtw_recv.h RXDESC_SIZE/OFFSET (6 dwords)
FCS_LEN = 4                 # IEEE80211_FCS_LEN
RSSI_UNKNOWN = 0


class RxDesc(NamedTuple):
    pkt_len: int            # MPDU length incl. the HW-appended FCS
    crc_err: bool
    icv_err: bool
    drvinfo_sz: int         # PHY-status size in bytes (desc nibble * 8)
    shift_sz: int
    physt: bool             # PHY-status present (drvinfo carries RSSI)
    rpt_sel: bool           # C2H firmware report, not an 802.11 frame
    data_rate: int          # DESC rate index (<= 3 => CCK)


def query_rx_desc(desc: bytes) -> RxDesc:
    """[SRC] rtl8812_query_rx_desc_status / rtl8812a_recv.h:64-105.

    dword0: pkt_len[13:0], crc[14], icv[15], drvinfo_sz[19:16], shift[25:24],
    physt[26]; dword2: rpt_sel[28]; dword3: rx_rate[6:0].
    """
    dw0 = int.from_bytes(desc[0:4], "little")
    dw2 = int.from_bytes(desc[8:12], "little")
    dw3 = int.from_bytes(desc[12:16], "little")
    return RxDesc(
        pkt_len=dw0 & 0x3FFF,
        crc_err=bool((dw0 >> 14) & 1),
        icv_err=bool((dw0 >> 15) & 1),
        drvinfo_sz=((dw0 >> 16) & 0xF) * 8,
        shift_sz=(dw0 >> 24) & 0x3,
        physt=bool((dw0 >> 26) & 1),
        rpt_sel=bool((dw2 >> 28) & 1),
        data_rate=dw3 & 0x7F,
    )


def _rnd8(x: int) -> int:
    return (x + 7) & ~7


def iter_frames(buf: bytes,
                decode_rssi: Callable[[bytes, int], int]) -> Iterator[Tuple[bytes, int]]:
    """[SRC] recvbuf2recvframe — walk the aggregated bulk-IN buffer.

    Yields (frame, rssi_dbm) for each good NORMAL_RX MPDU, FCS stripped. ``decode_rssi``
    is the per-chip PHY-status RSSI formula. C2H reports and crc/icv-error frames are
    skipped but still advance the walk; only a malformed length ends it.
    """
    transfer_len = len(buf)
    off = 0
    while transfer_len >= RXDESC_SIZE:
        d = query_rx_desc(buf[off:off + RXDESC_SIZE])
        pkt_offset = RXDESC_SIZE + d.drvinfo_sz + d.shift_sz + d.pkt_len
        if d.pkt_len <= 0 or pkt_offset > transfer_len:
            break
        if not (d.crc_err or d.icv_err or d.rpt_sel):
            start = off + RXDESC_SIZE + d.drvinfo_sz + d.shift_sz
            frame = buf[start:start + d.pkt_len]
            if len(frame) > FCS_LEN:
                rssi = (decode_rssi(buf[off + RXDESC_SIZE:start], d.data_rate)
                        if d.physt else RSSI_UNKNOWN)
                yield frame[:-FCS_LEN], rssi
        pkt_offset = _rnd8(pkt_offset)
        off += pkt_offset
        transfer_len -= pkt_offset
