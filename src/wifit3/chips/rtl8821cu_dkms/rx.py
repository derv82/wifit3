"""RTL8821CU (8821cu_dkms) RX path — bulk-IN buffer decode, vendor port.

[SRC] hal/rtl8821c/rtl8821c_rxdesc.c ``rtl8821c_query_rx_desc_status`` + the USB
``recvbuf2recvframe`` [SRC] os_dep/linux/usb_ops_linux.c. One bulk-IN transfer carries
several USB-aggregated RX packets, each:

    [ 24 B rx_pkt_desc ][ drvinfo_sz PHY-status ][ shift_sz pad ][ pkt_len MPDU ]

rounded up to an 8-byte boundary. The monitor RCR is 0x90000001 (RCR_APP_FCS = BIT31 set),
so the HW appends the 4-byte FCS; it is stripped here before the frame is yielded, so every
wifit3 driver delivers FCS-stripped frames (frame_end == MPDU_end).

The rx_pkt_desc dword layout is the HALMAC NIC format ([SRC] rtl8821c_rxdesc2attribute
rtl8821c_ops.c). RSSI comes from the PHY-status report, which on 8821C is the PHYDM Jaguar-2
``phy_sts_rpt_jgr2_type0/1`` — 8821C is PHYSTS_2ND_TYPE_IC, parsed by ``phydm_rx_physts_2nd_type``
([SRC] phydm_phystatus.c), NOT the Jaguar-1 ``phy_status_rpt_8812``. CCK vs OFDM is the report's
page nibble (byte 0), not the data rate. C2H firmware reports (rpt_sel) and crc/icv-error frames
are skipped.
"""
from __future__ import annotations

from typing import Iterator, NamedTuple, Tuple

RXDESC_SIZE = 24            # [SRC] rtw_recv.h RXDESC_SIZE (6 dwords)
FCS_LEN = 4                 # IEEE80211_FCS_LEN
_RSSI_UNKNOWN = 0


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
    """rtl8821c_query_rx_desc_status. dword0: pkt_len[13:0], crc[14], icv[15],
    drvinfo_sz[19:16], shift[25:24], physt[26]; dword2: rpt_sel[28]; dword3: rx_rate[6:0]."""
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


# CCK old-AGC LNA gain by 4-bit LNA index. [SRC] phydm_cck_rssi_8821c phydm_hal_api8821c.c
# (cck_agc_report_type 1 = the 4-bit table; this rfe-0x22 card defaults to the BTG RF set, which
# forces report_type 1 — [SRC] phydm_cck_lna_bit_num_chk phydm.c).
_CCK_LNA_GAIN = (10, 6, 2, -2, -6, -10, -14, -17, -20, -24, -28, -31, -34, -37, -40, -44)


def _cck_rssi(lna_idx: int, vga_idx: int) -> int:
    """CCK signal power (dBm) for the old-AGC path: 4-bit LNA gain minus the 5-bit VGA back-off.
    [SRC] phydm_cck_rssi_8821c phydm_hal_api8821c.c."""
    return _CCK_LNA_GAIN[lna_idx] - 2 * vga_idx


def decode_rssi(phy_status: bytes, cck_new_agc: bool) -> int:
    """Per-frame RSSI (dBm) from the PHYDM Jaguar-2 PHY-status report. The page nibble (byte 0)
    picks the struct: page 0 = CCK (``phy_sts_rpt_jgr2_type0``), page 1/2 = OFDM/HT/VHT
    (``phy_sts_rpt_jgr2_type1``). [SRC] phydm_get_phy_sts_type0 / phydm_get_phy_sts_type1
    phydm_phystatus.c.

    OFDM is per-path ``pwdb[0]`` (path A, byte 1) - 110. CCK with the new CCK-AGC latch
    (0xa9c[17]) is ``pwdb`` (byte 1) - 100; otherwise the old-AGC 4-bit LNA (byte 13[7:5] | byte
    14[7]) + 5-bit VGA (byte 13[4:0]). This 1T1R card reads ``cck_new_agc`` False, so CCK takes
    the LNA/VGA path."""
    if len(phy_status) < 2:
        return _RSSI_UNKNOWN
    page = phy_status[0] & 0xF
    if page == 0:
        if cck_new_agc:
            return phy_status[1] - 100
        if len(phy_status) < 15:
            return _RSSI_UNKNOWN
        b13, b14 = phy_status[13], phy_status[14]
        vga_idx = b13 & 0x1F
        lna_idx = ((b14 >> 7) << 3) | (b13 >> 5)        # 4-bit LNA = lna_h<<3 | lna_l
        return _cck_rssi(lna_idx, vga_idx)
    return phy_status[1] - 110


def _rnd8(x: int) -> int:
    return (x + 7) & ~7


def iter_frames(buf: bytes, cck_new_agc: bool = False) -> Iterator[Tuple[bytes, int]]:
    """recvbuf2recvframe — walk the aggregated bulk-IN buffer, yielding (frame, rssi_dbm) for each
    good NORMAL_RX MPDU, FCS stripped. C2H reports and crc/icv-error frames are skipped but still
    advance the walk; only a malformed length ends it. The PHY-status passed to ``decode_rssi`` is
    the ``drvinfo_sz`` block right after the desc (the ``shift_sz`` pad sits after it, before the
    MPDU)."""
    transfer_len = len(buf)
    off = 0
    while transfer_len >= RXDESC_SIZE:
        d = query_rx_desc(buf[off:off + RXDESC_SIZE])
        pkt_offset = RXDESC_SIZE + d.drvinfo_sz + d.shift_sz + d.pkt_len
        if d.pkt_len <= 0 or pkt_offset > transfer_len:
            break
        if not (d.crc_err or d.icv_err or d.rpt_sel):
            phystart = off + RXDESC_SIZE
            start = phystart + d.drvinfo_sz + d.shift_sz
            frame = buf[start:start + d.pkt_len]
            if len(frame) > FCS_LEN:
                rssi = (decode_rssi(buf[phystart:phystart + d.drvinfo_sz], cck_new_agc)
                        if d.physt else _RSSI_UNKNOWN)
                yield frame[:-FCS_LEN], rssi
        pkt_offset = _rnd8(pkt_offset)
        off += pkt_offset
        transfer_len -= pkt_offset
