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
``(frame, rssi)``. ``iter_tx_report2`` yields the TX_REPORT2 packets for ``rate_adaptive``.
"""
from __future__ import annotations

from typing import Iterator, NamedTuple, Tuple

from .phy_cond import ODM_CUT_I

RXDESC_SIZE = 24            # [SRC] rtw_recv.h RXDESC_SIZE (6 dwords); == RXDESC_OFFSET
FCS_LEN = 4                 # IEEE80211_FCS_LEN
NORMAL_RX = 0              # [SRC] rtw_recv.h:631 pkt_rpt_type enum
TX_REPORT1 = 1
TX_REPORT2 = 2
TX_RPT1_PKT_LEN = 8        # [SRC] rtl8188e_recv.h:57
_RSSI_UNKNOWN = 0          # reported when a frame carries no PHY status (physt=0)

# [SRC] phydm_cck_rssi_8188e (phydm_phystatus.c:833-835)
lna_gain_table_0 = (17, -1, -13, -29, -32, -35, -38, -41)
lna_gain_table_1 = (29, 20, 12, 3, -6, -15, -24, -33)
lna_gain_table_2 = (17, -1, -13, -17, -32, -43, -38, -47)


class RxDesc(NamedTuple):
    pkt_len: int            # MPDU length incl. the HW-appended FCS
    crc_err: bool
    icv_err: bool
    drvinfo_sz: int         # PHY-status size in bytes (desc nibble * 8)
    shift_sz: int           # extra rx-shift pad
    physt: bool             # PHY-status present (drvinfo carries RSSI)
    pkt_rpt_type: int       # NORMAL_RX vs a TX/HISR report (not an 802.11 frame)
    data_rate: int          # DESC rate index (<= 3 => CCK)
    mac_id_valid_entry0: int    # TX_REPORT2 only
    mac_id_valid_entry1: int


def query_rx_desc(desc: bytes) -> RxDesc:
    """[SRC] rtl8188e_query_rx_desc_status (rtl8188e_rxdesc.c:20) — decode the 24-byte RX status desc.

    The attrib starts zeroed (:42); only NORMAL_RX reads drvinfo/shift/physt/icv/rate from it.
    """
    dw0 = int.from_bytes(desc[0:4], "little")
    dw3 = int.from_bytes(desc[12:16], "little")
    crc_err = bool((dw0 >> 14) & 1)
    pkt_rpt_type = (dw3 >> 14) & 0x3
    if pkt_rpt_type == NORMAL_RX:
        return RxDesc(
            pkt_len=dw0 & 0x3FFF,
            crc_err=crc_err,
            icv_err=bool((dw0 >> 15) & 1),
            drvinfo_sz=((dw0 >> 16) & 0xF) * 8,
            shift_sz=(dw0 >> 24) & 0x3,
            physt=bool((dw0 >> 26) & 1),
            pkt_rpt_type=pkt_rpt_type,
            data_rate=dw3 & 0x3F,
            mac_id_valid_entry0=0,
            mac_id_valid_entry1=0,
        )
    mac_id_valid_entry0 = 0
    mac_id_valid_entry1 = 0
    if pkt_rpt_type == TX_REPORT1:
        pkt_len = TX_RPT1_PKT_LEN
    elif pkt_rpt_type == TX_REPORT2:
        pkt_len = dw0 & 0x3FF
        mac_id_valid_entry0 = int.from_bytes(desc[16:20], "little")
        mac_id_valid_entry1 = int.from_bytes(desc[20:24], "little")
    else:
        pkt_len = dw0 & 0x3FFF
    return RxDesc(
        pkt_len=pkt_len,
        crc_err=crc_err,
        icv_err=False,
        drvinfo_sz=0,
        shift_sz=0,
        physt=False,
        pkt_rpt_type=pkt_rpt_type,
        data_rate=0,
        mac_id_valid_entry0=mac_id_valid_entry0,
        mac_id_valid_entry1=mac_id_valid_entry1,
    )


def phydm_cck_rssi_8188e(cut_version: int, ext_lna: bool, type_glna: int,
                         lna_idx: int, vga_idx: int) -> int:
    """[SRC] phydm_phystatus.c:827. cut >= ODM_CUT_I is SMIC silicon, else TSMC."""
    if cut_version >= ODM_CUT_I:
        if ext_lna and type_glna == 0x2:            # eLNA 14dB
            lna_gain = lna_gain_table_2[lna_idx]
        else:
            lna_gain = lna_gain_table_0[lna_idx]
    else:
        lna_gain = lna_gain_table_1[lna_idx]
    return lna_gain - 2 * vga_idx


def decode_rssi(phy_status: bytes, data_rate: int, cut_version: int, ext_lna: bool,
                type_glna: int) -> int:
    """Per-frame RSSI in dBm (recv_signal_power) from ``phy_status_rpt_8192cd``
    [SRC] phydm_rx_phy_status92c_series_parsing. CCK (rate <= 3): the CCK AGC report
    (byte 5) gives LNA[7:5]/VGA[4:0] -> ``phydm_cck_rssi_8188e``. OFDM/HT: the combined
    pwdb byte (byte 4) gives ``((pwdb >> 1) & 0x7f) - 110``.
    """
    if len(phy_status) < 6:
        return _RSSI_UNKNOWN
    if data_rate <= 3:                              # CCK (1/2/5.5/11 Mbps)
        cck = phy_status[5]
        return phydm_cck_rssi_8188e(cut_version, ext_lna, type_glna,
                                    (cck & 0xE0) >> 5, cck & 0x1F)
    return ((phy_status[4] >> 1) & 0x7F) - 110      # OFDM/HT pwdb_all


def _rnd4(x: int) -> int:
    return (x + 3) & ~3


def _iter_packets(buf: bytes) -> Iterator[Tuple[int, RxDesc]]:
    """[SRC] recvbuf2recvframe — ``(offset, desc)`` per crc/icv-clean packet of the aggregated
    bulk-IN buffer. Error packets are skipped but advance the walk; only a malformed length ends it."""
    transfer_len = len(buf)
    off = 0
    while transfer_len >= RXDESC_SIZE:
        d = query_rx_desc(buf[off:off + RXDESC_SIZE])
        pkt_offset = RXDESC_SIZE + d.drvinfo_sz + d.shift_sz + d.pkt_len
        if d.pkt_len <= 0 or pkt_offset > transfer_len:
            break                               # malformed length: next boundary unknown
        if not (d.crc_err or d.icv_err):
            yield off, d
        pkt_offset = _rnd4(pkt_offset)          # RX_AGG_USB 4-byte alignment
        off += pkt_offset
        transfer_len -= pkt_offset


def iter_frames(buf: bytes, cut_version: int, ext_lna: bool,
                type_glna: int) -> Iterator[Tuple[bytes, int]]:
    """``(frame, rssi_dbm)`` for each NORMAL_RX MPDU, FCS stripped."""
    for off, d in _iter_packets(buf):
        if d.pkt_rpt_type != NORMAL_RX:
            continue
        start = off + RXDESC_SIZE + d.drvinfo_sz + d.shift_sz
        frame = buf[start:start + d.pkt_len]
        if len(frame) > FCS_LEN:                # strip the HW-appended FCS
            rssi = (decode_rssi(buf[off + RXDESC_SIZE:start], d.data_rate,
                                cut_version, ext_lna, type_glna)
                    if d.physt else _RSSI_UNKNOWN)
            yield frame[:-FCS_LEN], rssi


def iter_tx_report2(buf: bytes) -> Iterator[Tuple[bytes, int, int, int]]:
    """``(tx_rpt_buf, tx_rpt_len, mac_id_valid_entry0, mac_id_valid_entry1)`` per TX_REPORT2 packet:
    the odm_ra_tx_rpt2_handle_8188e arguments (usb_ops_linux.c:199)."""
    for off, d in _iter_packets(buf):
        if d.pkt_rpt_type == TX_REPORT2:
            start = off + RXDESC_SIZE
            yield buf[start:start + d.pkt_len], d.pkt_len, d.mac_id_valid_entry0, d.mac_id_valid_entry1
