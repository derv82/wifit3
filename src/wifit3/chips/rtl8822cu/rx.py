"""RTL8822CU RX stream decoding.

RTL8822C uses the rtw88 24-byte RX descriptor layout.  Its PHY-status page
0/1 PWDB fields match the 2T rtw88 layout used by the 8822B receiver.
"""
from __future__ import annotations

from wifit3.chips.rtw88_base.rx_common import (
    Endpoints,
    RxPktStat,
    iter_bulk_frames as _iter_bulk_frames,
    parse_rx_pkt_desc,
    probe_endpoints,
    read_rx_burst,
)


def _phy_rssi(buf: bytes, offset: int, _stat: RxPktStat) -> int | None:
    if len(buf) - offset < 3:
        return None
    page = buf[offset] & 0xF
    if page == 0:
        return max(-120, min(0, buf[offset + 1] - 110))
    if page == 1:
        return max(-120, min(0, max(buf[offset + 1], buf[offset + 2]) - 110))
    return None


def iter_bulk_frames(buf: bytes):
    return _iter_bulk_frames(buf, phy_status_rssi=_phy_rssi)


__all__ = ["Endpoints", "RxPktStat", "iter_bulk_frames", "parse_rx_pkt_desc", "probe_endpoints", "read_rx_burst"]
