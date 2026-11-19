# SPDX-License-Identifier: GPL-2.0-only
"""RTL8192EU RX path (rtl8xxxu core.c v6.6): rxdesc24 + rtl8723au PHY status.

RXDesc24/extract_mpdu decode a single descriptor/frame offline; rtl8xxxu_parse_rxdesc24 is the
runtime reader the driver feeds bulk-IN URBs. 8192eu's fops wires parse_phystats =
rtl8723au_rx_parse_phystats and cck_rssi = rtl8192e_cck_rssi.
"""
from collections.abc import Iterator
from dataclasses import dataclass

import usb.core

from . import constants as C
from .core import Rtl8xxxuPriv

RXDESC24_SIZE = 24
IEEE80211_MAX_FRAME_LEN = 2352  # include/linux/ieee80211.h
RTL8XXXU_RX_URB_SIZE = IEEE80211_MAX_FRAME_LEN + RXDESC24_SIZE  # core.c:6540, rx_buf_aggregation=0
C2H_8723B_RA_REPORT = 0x0C  # rtl8xxxu.h enum c2h_evt_8723b


@dataclass(frozen=True)
class RXDesc24:
    packet_length: int
    crc_error: bool
    icv_error: bool
    driver_info_units: int
    shift: int
    phy_stats: bool
    rpt_sel: bool
    rate: int
    tsf_low: int

    @classmethod
    def parse(cls, data: bytes) -> "RXDesc24":
        if len(data) < RXDESC24_SIZE:
            raise ValueError("short RTL8192EU RX descriptor")
        dw0 = int.from_bytes(data[:4], "little")
        dw2 = int.from_bytes(data[8:12], "little")
        dw3 = int.from_bytes(data[12:16], "little")
        dw5 = int.from_bytes(data[20:24], "little")
        return cls(
            packet_length=dw0 & 0x3fff,
            crc_error=bool(dw0 & (1 << 14)),
            icv_error=bool(dw0 & (1 << 15)),
            driver_info_units=(dw0 >> 16) & 0xf,
            shift=(dw0 >> 24) & 3,
            phy_stats=bool(dw0 & (1 << 26)),
            rpt_sel=bool(dw2 & (1 << 28)),
            rate=dw3 & 0x7f,
            tsf_low=dw5,
        )

    @property
    def payload_offset(self) -> int:
        # Driver-info size is in eight-byte units (rtl8xxxu core RX processing).
        return RXDESC24_SIZE + self.driver_info_units * 8 + self.shift


def extract_mpdu(urb: bytes) -> tuple[RXDesc24, bytes]:
    """Extract one descriptor/frame pair, rejecting short URBs and bad CRC/ICV.

    Not a complete aggregated-URB parser. Does not claim a valid FCS or RX PHY.
    """
    desc = RXDesc24.parse(urb)
    end = desc.payload_offset + desc.packet_length
    if end > len(urb):
        raise ValueError("RTL8192EU RX payload exceeds URB")
    if desc.crc_error or desc.icv_error:
        raise ValueError("RTL8192EU RX CRC/ICV error")
    return desc, urb[desc.payload_offset:end]


def rtl8192e_cck_rssi(priv: Rtl8xxxuPriv, phy_stats: bytes) -> int:
    """8192e.c:1688, on struct rtl8723au_phy_stats (cck_agc_rpt_ofdm_cfosho_a at byte 5)."""
    lna_gain_table_0 = (15, 9, -10, -21, -23, -27, -43, -44)
    lna_gain_table_1 = (24, 18, 13, -4, -11, -18, -31, -36)
    cck_agc_rpt = phy_stats[5]
    lna_idx = (cck_agc_rpt & C.CCK_AGC_RPT_LNA_IDX_MASK) >> 5  # GENMASK(7, 5)
    vga_idx = cck_agc_rpt & C.CCK_AGC_RPT_VGA_IDX_MASK         # GENMASK(4, 0)
    lna_gain = lna_gain_table_0[lna_idx] if priv.cck_agc_report_type == 0 else lna_gain_table_1[lna_idx]
    return lna_gain - (2 * vga_idx)


def rtl8723au_rx_parse_phystats(priv: Rtl8xxxuPriv, phy_stats: bytes, rxmcs: int) -> int:
    """core.c:1588; rx_status->signal only. parse_cfo needs an associated STA vif, never true in
    monitor, so it is omitted. cck_sig_qual_ofdm_pwdb_all is byte 4 of rtl8723au_phy_stats."""
    if rxmcs < C.DESC_RATE_6M:
        return priv.fops.cck_rssi(priv, phy_stats)
    return (phy_stats[4] >> 1) - 110


def rtl8xxxu_parse_rxdesc24(priv: Rtl8xxxuPriv, skb: bytes) -> Iterator[tuple[bytes, int | None]]:
    """core.c:6230. (frame, signal dBm or None for RX_FLAG_NO_SIGNAL_VAL); rpt_sel packets go to
    priv.c2hcmd_queue. crc32 frames are not yielded: mac80211 drops RX_FLAG_FAILED_FCS_CRC on a
    monitor without MONITOR_FLAG_FCSFAIL (net/mac80211/rx.c ieee80211_rx_monitor).
    """
    urb_len = len(skb)
    if urb_len < RXDESC24_SIZE:
        return

    offset = 0
    while True:
        rx_desc = RXDesc24.parse(skb[offset:offset + RXDESC24_SIZE])
        drvinfo_sz = rx_desc.driver_info_units * 8
        pkt_offset = (rx_desc.packet_length + drvinfo_sz + rx_desc.shift + RXDESC24_SIZE + 7) // 8 * 8
        has_next = urb_len >= pkt_offset + RXDESC24_SIZE

        phy_stats_start = offset + RXDESC24_SIZE
        frame_start = phy_stats_start + drvinfo_sz + rx_desc.shift
        frame = skb[frame_start:frame_start + rx_desc.packet_length]

        if rx_desc.rpt_sel:
            priv.c2hcmd_queue.append(frame)
        else:
            signal: int | None = None
            if rx_desc.phy_stats:
                signal = rtl8723au_rx_parse_phystats(priv, skb[phy_stats_start:frame_start], rx_desc.rate)
            if not rx_desc.crc_error:
                yield frame, signal

        if not has_next:
            return
        offset += pkt_offset
        urb_len -= pkt_offset
        if urb_len < RXDESC24_SIZE:
            return


def rtl8xxxu_c2hcmd_callback(priv: Rtl8xxxuPriv) -> None:
    """core.c:6162 c2hcmd_work. BT_INFO is unreachable (8192eu has no Bluetooth); RA_REPORT only
    feeds the cosmetic ra_report that nothing reads in monitor mode. Drain the queue so it cannot
    grow. TODO: verify, untested here — RA_REPORT stats need an associated vif, which wifit3 never has.
    """
    priv.c2hcmd_queue.clear()


def read_burst(dev: usb.core.Device, endpoint: int, timeout_ms: int = 100) -> bytes | None:
    """One bulk-IN URB (rtl8xxxu_submit_rx_urb's skb_size); None on timeout."""
    try:
        return bytes(dev.read(endpoint, RTL8XXXU_RX_URB_SIZE, timeout_ms))
    except usb.core.USBTimeoutError:
        return None
    except usb.core.USBError as error:
        if error.errno in (110, 10060):  # ETIMEDOUT / WSAETIMEDOUT
            return None
        raise
