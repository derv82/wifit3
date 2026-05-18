"""RTL8822BU RX-side glue.

Re-uses :mod:`wifit3.chips.rtw88_base.rx_common` for endpoint probing and
the 24-byte rx_pkt_desc decoder. Adds an 8822b-specific phy_status RSSI
parser (pages 0/1, mirrors `rtw8822b.c:query_phy_status`).
"""

from __future__ import annotations

import struct

from wifit3.chips.rtw88_base.rx_common import (  # noqa: F401  (re-exports)
    RX_PKT_DESC_SZ,
    Endpoints,
    RxPktStat,
    iter_bulk_frames as _shared_iter_bulk_frames,
    parse_rx_pkt_desc,
    probe_endpoints,
    read_rx_burst,
)


def parse_phy_status_rssi_8822b(
    buf: bytes, offset: int, _stat: RxPktStat
) -> int | None:
    """Mirror `rtw8822b.c:query_phy_status_page0` / `_page1` for RSSI.

    Returns approximate RSSI in dBm. Page 0 (CCK) reports a single PWDB at
    byte 1 of the phy_status; page 1 (OFDM/HT/VHT) reports PWDB_A and
    PWDB_B at bytes 1 and 2 — we take the max for a "what the receiver saw"
    estimate. The phy_status page byte is self-describing, so RxPktStat is
    unused here (signature matches the shared PhyStatusRssi callback).
    """
    if len(buf) - offset < 4:
        return None
    page = buf[offset] & 0xF
    if page == 0:
        pwdb = buf[offset + 1]
        return pwdb - 110
    if page == 1:
        if len(buf) - offset < 4:
            return None
        pwdb_a = buf[offset + 1]
        pwdb_b = buf[offset + 2]
        return max(pwdb_a, pwdb_b) - 110
    return None


def iter_bulk_frames(buf: bytes):
    """Wrapper that hooks the 8822b phy_status RSSI parser into the
    shared iterator."""
    return _shared_iter_bulk_frames(
        buf, phy_status_rssi=parse_phy_status_rssi_8822b
    )
