"""RTL8821AU (DKMS) RX path — bulk-IN buffer decode, vendor port.

Mirrors `rtl8812_query_rx_desc_status` [SRC] rtl8812a_rxdesc.c + `recvbuf2recvframe`
[SRC] usb/usb_ops_linux.c:104. One bulk-IN transfer carries several USB-aggregated
RX packets, each:

    [ 24 B RX status desc ][ drvinfo_sz PHY-status ][ shift_sz pad ][ pkt_len MPDU ]

rounded up to an 8-byte boundary. The MPDU's trailing 4-byte FCS is HW-appended
(monitor RCR sets RCR_APPFCS) and stripped here before the frame is yielded — every
wifit3 driver delivers FCS-stripped frames so the parser can trust frame_end==MPDU_end.

The RX-status-desc layout is identical to the 8814au sibling; the PHY-status RSSI
formulas are 8821a-specific (see decode_rssi). The 8821a monitor RCR clears
RCR_ACRC32|RCR_AICV, so the HW drops CRC/ICV-error frames before the FIFO — the
in-walk crc/icv skip below is then just defensive.
"""
from __future__ import annotations

import struct
from typing import Iterator, NamedTuple, Tuple

RXDESC_SIZE = 24            # [SRC] rtw_recv.h RXDESC_SIZE/OFFSET (6 dwords)
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


def query_rx_desc(buf: bytes, off: int = 0) -> RxDesc:
    """[SRC] rtl8812_query_rx_desc_status / rtl8812a_recv.h:64-105.

    dword0: pkt_len[13:0], crc[14], icv[15], drvinfo_sz[19:16], shift[25:24],
    physt[26]; dword2: rpt_sel[28]; dword3: rx_rate[6:0].
    """
    dw0, _, dw2, dw3 = struct.unpack_from("<IIII", buf, off)
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


def _cck_rssi_8821a(lna_idx: int, vga_idx: int) -> int:
    """[SRC] phydm_cck_rssi_8821a (phydm_rtl8821a.c:26) — CCK signal power (dBm)."""
    base = {5: -38, 4: -30, 2: -17, 1: -1, 0: 15}.get(lna_idx)
    return 0 if base is None else base - 2 * vga_idx


def decode_rssi(phy_status: bytes, data_rate: int) -> int:
    """Per-frame RSSI in dBm from the PHY-status struct.

    [SRC] phydm_rx_phy_status_jaguar_series_parsing (phydm_phystatus.c:835/932): CCK
    (rate<=3) reads the CCK AGC report (cck_agc_rpt_ofdm_cfosho_a, byte 5) -> lna/vga ->
    phydm_cck_rssi_8821a. OFDM reads pwdb_all (cck_sig_qual_ofdm_pwdb_all, byte 4) as
    ``((pwdb_all >> 1) & 0x7f) - 110`` — the byte is the AGC sum of both DC paths, so it
    is halved before the dBm conversion. (Beacons are CCK on 2.4 GHz but OFDM on 5 GHz,
    so a missing >>1 reads ~2x too strong and saturates 5 GHz to ~0 dBm.)
    """
    if len(phy_status) < 6:
        return _RSSI_UNKNOWN
    if data_rate <= 3:                              # CCK (1/2/5.5/11 Mbps)
        cck_agc_rpt = phy_status[5]
        return _cck_rssi_8821a((cck_agc_rpt & 0xE0) >> 5, cck_agc_rpt & 0x1F)
    return ((phy_status[4] >> 1) & 0x7F) - 110      # OFDM/HT/VHT pwdb_all (>>1 per phydm)


def _rnd8(x: int) -> int:
    return (x + 7) & ~7


def iter_frames(buf: bytes) -> Iterator[Tuple[bytes, int]]:
    """[SRC] recvbuf2recvframe — walk the aggregated bulk-IN buffer.

    Yields (frame, rssi_dbm) for each good NORMAL_RX MPDU, FCS stripped. C2H reports
    and crc/icv-error frames are skipped but still advance the walk; only a malformed
    length ends it.
    """
    transfer_len = len(buf)
    off = 0
    while transfer_len >= RXDESC_SIZE:
        dw0, _, dw2, dw3 = struct.unpack_from("<IIII", buf, off)
        pkt_len = dw0 & 0x3FFF
        drvinfo_sz = ((dw0 >> 16) & 0xF) * 8
        shift_sz = (dw0 >> 24) & 0x3
        pkt_offset = RXDESC_SIZE + drvinfo_sz + shift_sz + pkt_len
        if pkt_len <= 0 or pkt_offset > transfer_len:
            break
        if not (dw0 & 0xC000 or dw2 & 0x10000000) and pkt_len > FCS_LEN:
            phy_start = off + RXDESC_SIZE
            start = phy_start + drvinfo_sz + shift_sz
            if dw0 & (1 << 26) and drvinfo_sz >= 6:
                data_rate = dw3 & 0x7F
                if data_rate <= 3:
                    cck_agc_rpt = buf[phy_start + 5]
                    rssi = _cck_rssi_8821a((cck_agc_rpt & 0xE0) >> 5, cck_agc_rpt & 0x1F)
                else:
                    rssi = ((buf[phy_start + 4] >> 1) & 0x7F) - 110
            else:
                rssi = _RSSI_UNKNOWN
            yield buf[start:start + pkt_len - FCS_LEN], rssi
        pkt_offset = _rnd8(pkt_offset)
        off += pkt_offset
        transfer_len -= pkt_offset
