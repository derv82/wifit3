"""RTL8814AU RX path (M3b-3a) — bulk-IN buffer decode, vendor faithful.

Mirrors `rtl8814_query_rx_desc_status` [SRC rtl8814a_rxdesc.c] + `recvbuf2recvframe`
[SRC usb/usb_ops_linux.c:105]. One bulk-IN transfer may carry several USB-aggregated
RX packets, each laid out as:

    [ 24 B RX status desc ][ drvinfo_sz PHY-status ][ shift_sz pad ][ pkt_len MPDU ]

rounded up to an 8-byte boundary. The MPDU's trailing 4-byte FCS is appended by the
HW (the monitor RCR sets RCR_APPFCS).

Two DEVIATIONS from the vendor recv loop, both intentional for wifit3:
  * A crc/icv-error packet stops the rest of the buffer — the vendor's `mp_mode==0`
    behaviour (`goto _exit_recvbuf2recvframe`) — even though the monitor RCR accepts
    such frames into the FIFO (RCR_ACRC32|RCR_AICV).
  * The 4-byte FCS is stripped before the frame is yielded. The vendor *keeps* it in
    monitor mode (for radiotap), but every wifit3 driver delivers FCS-stripped frames
    so the parser/attacks can trust `frame_end == MPDU_end`.

RSSI (the PHY-status struct present when `physt=1`) is M3b-3b; this module yields
frames only.
"""
from __future__ import annotations

from typing import Iterator, NamedTuple

RXDESC_SIZE = 24            # [SRC] rtw_recv.h RXDESC_SIZE (6 dwords)
FCS_LEN = 4                 # IEEE80211_FCS_LEN


class RxDesc(NamedTuple):
    pkt_len: int            # MPDU length incl. the HW-appended FCS
    crc_err: bool
    icv_err: bool
    drvinfo_sz: int         # PHY-status size in bytes (desc nibble * 8)
    shift_sz: int           # extra rx-shift pad
    physt: bool             # PHY-status present (drvinfo carries RSSI)
    rpt_sel: bool           # C2H firmware report, not an 802.11 frame


def query_rx_desc(desc: bytes) -> RxDesc:
    """[SRC] rtl8814_query_rx_desc_status — decode the 24-byte RX status desc.

    Field bit positions [SRC rtl8814a_recv.h GET_RX_STATUS_DESC_*_8814A /
    rtl8814a_xmit.h RPT_SEL]: dword0 pkt_len[13:0], crc[14], icv[15],
    drvinfo_sz[19:16], shift[25:24], physt[26]; dword2 rpt_sel[28].
    """
    dw0 = int.from_bytes(desc[0:4], "little")
    dw2 = int.from_bytes(desc[8:12], "little")
    return RxDesc(
        pkt_len=dw0 & 0x3FFF,
        crc_err=bool((dw0 >> 14) & 1),
        icv_err=bool((dw0 >> 15) & 1),
        drvinfo_sz=((dw0 >> 16) & 0xF) * 8,
        shift_sz=(dw0 >> 24) & 0x3,
        physt=bool((dw0 >> 26) & 1),
        rpt_sel=bool((dw2 >> 28) & 1),
    )


def _rnd8(x: int) -> int:
    return (x + 7) & ~7


def iter_frames(buf: bytes) -> Iterator[bytes]:
    """[SRC] recvbuf2recvframe — walk the aggregated bulk-IN buffer.

    Yields each NORMAL_RX MPDU with its FCS stripped. C2H firmware reports are
    skipped (but still advance the walk); a crc/icv error or a malformed length
    ends the walk.
    """
    transfer_len = len(buf)
    off = 0
    while transfer_len >= RXDESC_SIZE:
        d = query_rx_desc(buf[off:off + RXDESC_SIZE])
        if d.crc_err or d.icv_err:
            break
        pkt_offset = RXDESC_SIZE + d.drvinfo_sz + d.shift_sz + d.pkt_len
        if d.pkt_len <= 0 or pkt_offset > transfer_len:
            break
        if not d.rpt_sel:                       # NORMAL_RX (not a C2H report)
            start = off + RXDESC_SIZE + d.drvinfo_sz + d.shift_sz
            frame = buf[start:start + d.pkt_len]
            if len(frame) > FCS_LEN:            # strip the HW-appended FCS
                yield frame[:-FCS_LEN]
        pkt_offset = _rnd8(pkt_offset)          # jaguar 8-byte alignment
        off += pkt_offset
        transfer_len -= pkt_offset
