"""RTL8922AU RX: split a bulk-IN transfer into 802.11 MPDUs, stripping the rtw89 BE rx descriptor.

One bulk-IN transfer carries N packets, each a fixed rx descriptor (24B short / 40B long) plus
optional shift/drv-info/phy-report/hdr-conv regions, then the MPDU (FCS-inclusive). Packets are
16-byte aligned (the 8922A rx_agg_alignment). Only pkt_type 0 (WIFI) is an 802.11 frame; the rest
are firmware/PPDU-status reports. [SRC] core.c:4162 query_rxdesc_v2, usb.c:477, rtw8922au.c:17.
"""
import struct

RX_RECVBUF_SZ = 20480                    # RTW89_USB_RECVBUF_SZ. usb.h:14
_RX_TYPE_WIFI = 0                        # RTW89_CORE_RX_TYPE_WIFI. core.h:388
_MAX_RSSI = 110                          # MAX_RSSI. core.h:215


def _rssi_dbm(rssi: int) -> int | None:
    """rtw8922a_phy_rpt_to_rssi: the 12-bit hw RSSI to dBm, or None if there is no signal. [SRC]
    rtw8922a.c:3033."""
    if rssi <= 1 or (rssi >> 2) > _MAX_RSSI:
        return None
    return (rssi >> 2) - _MAX_RSSI


def iter_bulk_frames(buf: bytes):
    """Yield (stat, mpdu, rssi) per WIFI frame in a bulk-IN transfer. mpdu is the FCS-stripped
    802.11 MPDU; rssi is dBm or None. [SRC] core.c:4162-4235, usb.c:477-505."""
    pos, n = 0, len(buf)
    while pos + 24 <= n:
        w0 = struct.unpack_from("<I", buf, pos)[0]
        rxd_len = 40 if (w0 >> 31) & 1 else 24            # long_rxdesc -> 40B, else short 24B
        pkt_size = w0 & 0x3FFF
        shift_len = ((w0 >> 14) & 0x3) << 1
        drv_info_len = ((w0 >> 18) & 0x3) << 3
        hdr_cnv_len = ((w0 >> 20) & 0x3) << 4
        phy_rpt_len = ((w0 >> 22) & 0x3) << 3
        pkt_type = (w0 >> 24) & 0x3F
        mpdu_off = rxd_len + shift_len + drv_info_len + phy_rpt_len + hdr_cnv_len
        total = mpdu_off + pkt_size
        if pkt_size == 0 or pos + total > n:              # padding / truncated tail
            break
        if pkt_type == _RX_TYPE_WIFI:
            rssi = None
            if phy_rpt_len >= 8:                          # inline phy report holds the RSSI
                rpt = struct.unpack_from("<I", buf, pos + rxd_len)[0]
                rssi = _rssi_dbm(rpt & 0xFFF)
            mpdu = bytes(buf[pos + mpdu_off:pos + mpdu_off + max(pkt_size - 4, 0)])
            yield None, mpdu, rssi
        pos += (total + 15) & ~15                         # ALIGN(pkt_offset + pkt_size, 16)
