"""RTL8188EUS RX path — bulk-IN buffer decode.

Mirrors ``rtl8188e_query_rx_desc_status`` [SRC] rtl8188e_rxdesc.c:20 +
``recvbuf2recvframe`` [SRC] usb/usb_ops_linux.c:108. One bulk-IN transfer carries one or
more USB-aggregated RX packets, each laid out as:

    [ 24 B RX status desc ][ drvinfo_sz PHY-status ][ shift_sz pad ][ pkt_len MPDU ]

advanced by ``_RND4(pkt_offset)`` (this card runs RX_AGG_USB). The MPDU's trailing 4-byte
FCS is appended by HW (the monitor RCR sets RCR_APPFCS).

Two DEVIATIONS from the vendor recv loop, both intentional for wifit3:
  * A crc/icv-error packet is skipped but the walk CONTINUES (the vendor STA bails:
    ``goto _exit_recvbuf2recvframe`` for ``mp_mode==0``). Keeps good frames aggregated
    after a bad one; only a malformed descriptor length ends the walk.
  * The 4-byte FCS is stripped before the frame is yielded (the vendor keeps it in
    monitor for radiotap). Every wifit3 driver delivers FCS-stripped frames so the
    parser/attacks can trust ``frame_end == MPDU_end`` [[project_rx_frames_include_fcs]].

When ``physt`` the RX desc is followed by the PHY-status struct (``phy_status_rpt_8192cd``,
the 11N report) from which ``decode_rssi`` derives the per-frame dBm; ``iter_frames`` yields
``(frame, rssi)``.
"""
from __future__ import annotations

from typing import Iterator, NamedTuple, Tuple

RXDESC_SIZE = 24            # [SRC] rtw_recv.h RXDESC_SIZE (6 dwords); == RXDESC_OFFSET
FCS_LEN = 4                 # IEEE80211_FCS_LEN
NORMAL_RX = 0              # [SRC] rtw_recv.h pkt_rpt_type enum (vs TX_REPORT1/2, HIS_REPORT)
_RSSI_UNKNOWN = 0          # reported when a frame carries no PHY status (physt=0)

# CCK LNA gain table for this card's cut (A => TSMC branch), lna_gain_table_1
# [SRC] phydm_cck_rssi_8188e. (cut >= I would use lna_gain_table_0/2.)
_CCK_LNA_GAIN = (29, 20, 12, 3, -6, -15, -24, -33)


class RxDesc(NamedTuple):
    pkt_len: int            # MPDU length incl. the HW-appended FCS
    crc_err: bool
    icv_err: bool
    drvinfo_sz: int         # PHY-status size in bytes (desc nibble * 8)
    shift_sz: int           # extra rx-shift pad
    physt: bool             # PHY-status present (drvinfo carries RSSI)
    pkt_rpt_type: int       # NORMAL_RX vs a TX/HISR report (not an 802.11 frame)
    data_rate: int          # DESC rate index (<= 3 => CCK)


def query_rx_desc(desc: bytes) -> RxDesc:
    """[SRC] rtl8188e_query_rx_desc_status — decode the 24-byte RX status desc.

    rxdw0: pkt_len[13:0], crc[14], icv[15], drvinfo_sz[19:16], shift[25:24], physt[26];
    rxdw3: data_rate[5:0], pkt_rpt_type[15:14].
    """
    dw0 = int.from_bytes(desc[0:4], "little")
    dw3 = int.from_bytes(desc[12:16], "little")
    return RxDesc(
        pkt_len=dw0 & 0x3FFF,
        crc_err=bool((dw0 >> 14) & 1),
        icv_err=bool((dw0 >> 15) & 1),
        drvinfo_sz=((dw0 >> 16) & 0xF) * 8,
        shift_sz=(dw0 >> 24) & 0x3,
        physt=bool((dw0 >> 26) & 1),
        pkt_rpt_type=(dw3 >> 14) & 0x3,
        data_rate=dw3 & 0x3F,
    )


def decode_rssi(phy_status: bytes, data_rate: int) -> int:
    """Per-frame RSSI in dBm (recv_signal_power) from ``phy_status_rpt_8192cd``
    [SRC] phydm_rx_phy_status92c_series_parsing. CCK (rate <= 3): the CCK AGC report
    (byte 5) gives LNA[7:5]/VGA[4:0] -> ``lna_gain - 2*VGA``. OFDM/HT: the combined
    pwdb byte (byte 4) gives ``((pwdb >> 1) & 0x7f) - 110``.
    """
    if len(phy_status) < 6:
        return _RSSI_UNKNOWN
    if data_rate <= 3:                              # CCK (1/2/5.5/11 Mbps)
        cck = phy_status[5]
        return _CCK_LNA_GAIN[(cck & 0xE0) >> 5] - 2 * (cck & 0x1F)
    return ((phy_status[4] >> 1) & 0x7F) - 110      # OFDM/HT pwdb_all


def _rnd4(x: int) -> int:
    return (x + 3) & ~3


def iter_frames(buf: bytes) -> Iterator[Tuple[bytes, int]]:
    """[SRC] recvbuf2recvframe — walk the aggregated bulk-IN buffer.

    Yields ``(frame, rssi_dbm)`` for each good NORMAL_RX MPDU, FCS stripped. C2H/TX
    reports and crc/icv-error frames are skipped but still advance the walk; only a
    malformed length ends it.
    """
    transfer_len = len(buf)
    off = 0
    while transfer_len >= RXDESC_SIZE:
        d = query_rx_desc(buf[off:off + RXDESC_SIZE])
        pkt_offset = RXDESC_SIZE + d.drvinfo_sz + d.shift_sz + d.pkt_len
        if d.pkt_len <= 0 or pkt_offset > transfer_len:
            break                               # malformed length: next boundary unknown
        if d.pkt_rpt_type == NORMAL_RX and not (d.crc_err or d.icv_err):
            start = off + RXDESC_SIZE + d.drvinfo_sz + d.shift_sz
            frame = buf[start:start + d.pkt_len]
            if len(frame) > FCS_LEN:            # strip the HW-appended FCS
                rssi = (decode_rssi(buf[off + RXDESC_SIZE:start], d.data_rate)
                        if d.physt else _RSSI_UNKNOWN)
                yield frame[:-FCS_LEN], rssi
        pkt_offset = _rnd4(pkt_offset)          # RX_AGG_USB 4-byte alignment
        off += pkt_offset
        transfer_len -= pkt_offset
