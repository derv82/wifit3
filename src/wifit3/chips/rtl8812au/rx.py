"""RTL8812AU RX path — rate-aware Jaguar phy_status RSSI parser.

The rx_pkt_desc layout (24 bytes) and bulk-IN frame packing are family-
shared via `rtw88_base.rx_common`. 8812A uses Jaguar phy_status reports
(same as 8821A and 8814A); the RSSI field LIVES IN A DIFFERENT PLACE
depending on the frame's rate:

  * CCK rates (1M/2M/5.5M/11M, DESC_RATE0..3):
      lna_idx = w1[15:13], vga_idx = w1[12:8]
      rssi_dBm = rtw8812a_cck_rx_pwr(lna_idx, vga_idx)  (chip-specific lookup)
  * OFDM/HT/VHT rates (DESC_RATE6M and up):
      gain_a   = w0[6:0]
      gain_b   = w0[14:8]   (8812A is 2T2R; take max across paths)
      rssi_dBm = max(gain - 110) across active paths

Reference:
    rtw88xxa.c:1518   rtw88xxa_query_phy_status
    rtw88xxa.h:72..93 struct rtw_jaguar_phy_status_rpt + GENMASKs
    rtw8812a.c:19..56 rtw8812a_cck_rx_pwr
"""

from __future__ import annotations

import struct
from typing import Iterator

from wifit3.chips.rtw88_base.rx_common import (
    Endpoints,
    RxPktStat,
    iter_bulk_frames as _shared_iter_bulk_frames,
    parse_rx_pkt_desc,
    probe_endpoints,
    read_rx_burst,
)


DESC_RATE11M = 0x03   # last CCK rate; anything > this is OFDM/HT/VHT


def _rtw8812a_cck_rx_pwr(lna_idx: int, vga_idx: int) -> int:
    """Port of rtw8812a_cck_rx_pwr (rtw8812a.c:19..56). Returns dBm."""
    if lna_idx == 7:
        if vga_idx <= 27:
            return -94 + 2 * (27 - vga_idx)
        return -94
    if lna_idx == 6:
        return -42 + 2 * (2 - vga_idx)
    if lna_idx == 5:
        return -36 + 2 * (7 - vga_idx)
    if lna_idx == 4:
        return -30 + 2 * (7 - vga_idx)
    if lna_idx == 3:
        return -18 + 2 * (7 - vga_idx)
    if lna_idx == 2:
        return 2 * (5 - vga_idx)
    if lna_idx == 1:
        return 14 - 2 * vga_idx
    if lna_idx == 0:
        return 20 - 2 * vga_idx
    return 0


def parse_jaguar_phy_status_rssi(
    buf: bytes, offset: int, stat: RxPktStat,
) -> int | None:
    """Extract dBm RSSI from a Jaguar phy_status report.

    Rate-aware: CCK frames carry RSSI as lna_idx/vga_idx pair in w1;
    OFDM/HT/VHT carry per-path gain in w0[6:0] (path A) and w0[14:8]
    (path B). Returns approximate dBm or None if buffer is short.
    """
    if len(buf) - offset < 8:
        return None
    w0 = struct.unpack_from("<I", buf, offset)[0]
    w1 = struct.unpack_from("<I", buf, offset + 4)[0]

    if stat.rate <= DESC_RATE11M:
        # CCK branch: rtw88xxa.c:1532-1535
        vga_idx = (w1 >> 8) & 0x1F     # RTW_JGRPHY_W1_AGC_RPT_VGA_IDX
        lna_idx = (w1 >> 13) & 0x07    # RTW_JGRPHY_W1_AGC_RPT_LNA_IDX
        return _rtw8812a_cck_rx_pwr(lna_idx, vga_idx)

    # OFDM/HT/VHT branch: rtw88xxa.c:1543-1560
    # rx_power[i] = gain[i] - 110; signal_power = max(power_a, power_b)
    gain_a = w0 & 0x7F                 # RTW_JGRPHY_W0_GAIN_A
    gain_b = (w0 >> 8) & 0x7F          # RTW_JGRPHY_W0_GAIN_B
    rx_pwr_a = gain_a - 110
    rx_pwr_b = gain_b - 110
    # 2T2R card: take max across paths to mirror kernel's signal_power calc.
    return max(rx_pwr_a, rx_pwr_b)


def iter_bulk_frames(buf: bytes) -> Iterator[tuple[RxPktStat, bytes, int | None]]:
    """8812A frame iterator with the rate-aware Jaguar RSSI parser wired in."""
    yield from _shared_iter_bulk_frames(buf, phy_status_rssi=parse_jaguar_phy_status_rssi)


__all__ = [
    "Endpoints",
    "RxPktStat",
    "iter_bulk_frames",
    "parse_jaguar_phy_status_rssi",
    "parse_rx_pkt_desc",
    "probe_endpoints",
    "read_rx_burst",
]
