"""RTL8812AU RX path — thin wrapper over the family-shared rx_common.

The rx_pkt_desc layout (24 bytes) and bulk-IN frame packing are family-
shared via `rtw88_base.rx_common`. The only chip-specific bit is the
phy_status report format: 8812A uses `rtw_jaguar_phy_status_rpt` (same
as 8821A — both are 88xxA-gen Jaguar PHYs), so the RSSI extraction is
identical to 8821AU's.

Reference: rtw88xxa.c:1523 (phy-status rpt layout for Jaguar PHY).
"""

from __future__ import annotations

import struct
import usb.core
from typing import Iterator

from wifit3.chips.rtw88_base.rx_common import (
    Endpoints,
    RxPktStat,
    iter_bulk_frames as _shared_iter_bulk_frames,
    parse_rx_pkt_desc,
    probe_endpoints,
    read_rx_burst,
)


def parse_jaguar_phy_status_rssi(buf: bytes, offset: int) -> int | None:
    """Extract path-A RSSI from rtw_jaguar_phy_status_rpt.w1[6:0].

    Per rtw88xxa.c:1523, gain = w1 & 0x7F; rssi_dBm ≈ -95 + gain.
    """
    if len(buf) - offset < 8:
        return None
    w1 = struct.unpack_from("<I", buf, offset + 4)[0]
    gain = w1 & 0x7F
    return -95 + gain


def iter_bulk_frames(buf: bytes) -> Iterator[tuple[RxPktStat, bytes, int | None]]:
    """8812A frame iterator with the Jaguar RSSI parser wired in."""
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
