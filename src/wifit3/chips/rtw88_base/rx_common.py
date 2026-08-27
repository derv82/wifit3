"""Generic rtw88 RX-descriptor decoder + bulk-IN endpoint probe.

The 24-byte rx_pkt_desc layout is family-shared (`rtw_rx_desc`). The PHY-
status report that follows it varies by chip; chips supply their own
phy-status RSSI parser.

References:
    rx.c:264                  rtw_rx_query_rx_desc
    usb.c:238                 rtw_usb_parse
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Callable, Iterator

import usb.core

logger = logging.getLogger(__name__)


RX_PKT_DESC_SZ = 24


@dataclass(frozen=True)
class Endpoints:
    bulk_in: list[int]
    bulk_out: list[int]
    interrupt: list[int]

    @property
    def primary_bulk_in(self) -> int:
        if not self.bulk_in:
            raise RuntimeError("no bulk-IN endpoint found")
        return self.bulk_in[0]


def probe_endpoints(dev: usb.core.Device, *, configuration: int = 0,
                    interface: int = 0) -> Endpoints:
    """Enumerate the descriptor and classify pipes (mirrors rtw_usb_parse)."""
    cfg = dev.get_active_configuration()
    intf = cfg[(interface, 0)]
    bulk_in, bulk_out, interrupt = [], [], []
    for ep in intf:
        addr = ep.bEndpointAddress
        is_in = bool(addr & 0x80)
        attr = ep.bmAttributes & 0x03
        if attr == 0x02:  # bulk
            (bulk_in if is_in else bulk_out).append(addr)
        elif attr == 0x03 and is_in:  # interrupt
            interrupt.append(addr)
    logger.debug(
        "endpoints: bulk_in=%s bulk_out=%s interrupt=%s",
        [f"0x{e:02x}" for e in bulk_in],
        [f"0x{e:02x}" for e in bulk_out],
        [f"0x{e:02x}" for e in interrupt],
    )
    return Endpoints(bulk_in=bulk_in, bulk_out=bulk_out, interrupt=interrupt)


@dataclass(frozen=True)
class RxPktStat:
    pkt_len: int
    crc_err: bool
    icv_err: bool
    drv_info_sz: int      # in bytes (already multiplied by 8)
    shift: int            # in bytes
    phy_status_present: bool
    is_c2h: bool
    rate: int             # DESC_RATE_*
    bw: int               # bandwidth code
    tsf_low: int
    macid: int
    ppdu_cnt: int

    @property
    def mpdu_offset(self) -> int:
        """Byte offset of the MPDU within its rx_desc-prefixed buffer."""
        return RX_PKT_DESC_SZ + self.drv_info_sz + self.shift

    @property
    def total_size(self) -> int:
        return self.mpdu_offset + self.pkt_len


def parse_rx_pkt_desc(buf: bytes, offset: int = 0) -> RxPktStat:
    """Decode the 24-byte rx_pkt_desc at `buf[offset:offset+24]`."""
    if len(buf) - offset < RX_PKT_DESC_SZ:
        raise ValueError(
            f"rx_pkt_desc needs {RX_PKT_DESC_SZ} bytes, got {len(buf) - offset}"
        )
    w0, w1, w2, w3, w4, w5 = struct.unpack_from("<6I", buf, offset)
    return RxPktStat(
        pkt_len=w0 & 0x3FFF,
        crc_err=bool(w0 & (1 << 14)),
        icv_err=bool(w0 & (1 << 15)),
        drv_info_sz=((w0 >> 16) & 0xF) * 8,
        shift=(w0 >> 24) & 0x3,
        phy_status_present=bool(w0 & (1 << 26)),
        is_c2h=bool(w2 & (1 << 28)),
        rate=w3 & 0xFF,
        bw=(w4 >> 4) & 0x7,
        tsf_low=w5,
        macid=w1 & 0x1F,
        ppdu_cnt=(w2 >> 29) & 0x3,
    )


# A phy-status RSSI parser is per-chip; pass it in here.
# It receives the buffer, the offset where phy_status starts (= rx_desc_end),
# and the parsed RxPktStat (for rate-aware branching, e.g. CCK vs OFDM).
PhyStatusRssi = Callable[[bytes, int, "RxPktStat"], int | None]


def iter_bulk_frames(
    buf: bytes,
    *,
    phy_status_rssi: PhyStatusRssi | None = None,
) -> Iterator[tuple[RxPktStat, bytes, int | None]]:
    """Yield (pkt_stat, mpdu_bytes, rssi) tuples for each frame in `buf`.

    Frames are concatenated with `round_up(total_size, 8)` alignment.
    Skips C2H entries (firmware events, not 802.11 frames).
    """
    pos = 0
    while pos + RX_PKT_DESC_SZ <= len(buf):
        try:
            stat = parse_rx_pkt_desc(buf, pos)
        except ValueError:
            return

        if stat.pkt_len == 0 or stat.total_size == 0:
            return

        if pos + stat.total_size > len(buf):
            return

        rssi = None
        if (stat.phy_status_present and stat.drv_info_sz >= 8
                and phy_status_rssi is not None):
            rssi = phy_status_rssi(buf, pos + RX_PKT_DESC_SZ, stat)

        mpdu_start = pos + stat.mpdu_offset
        # rtw88 USB hardware reports pkt_len INCLUSIVE of the trailing 4-byte
        # 802.11 FCS. Strip it here so all consumers see only the on-air MPDU
        # body — leaving it on breaks length-sensitive code downstream
        # (e.g. WEP ARP detection at wlan/wep_store.py rejects ARP frames by
        # length, ChopChop's ICV slice expects body-end == cipher-end, etc.).
        # Confirmed via FCSDIAG CRC32 trailer tally on RTL8821AU (6000/6000
        # frames had a valid CRC32 tail). The 8812AU/8822BU/8814AU share this
        # descriptor format and almost certainly behave the same, but each
        # needs an independent HW test (scan + WEP + WPA + WPS) before
        # being marked verified.
        mpdu = bytes(buf[mpdu_start: mpdu_start + max(stat.pkt_len - 4, 0)])

        if not stat.is_c2h:
            yield (stat, mpdu, rssi)

        next_pos = (pos + stat.total_size + 7) & ~7
        if next_pos <= pos:
            return
        pos = next_pos


def read_rx_burst(dev: usb.core.Device, ep: int, *,
                  max_size: int = 16384, timeout_ms: int = 100) -> bytes | None:
    """Single bulk-IN read. Returns None on timeout, bytes on success."""
    try:
        data = dev.read(ep, max_size, timeout_ms)
        return bytes(data)
    except usb.core.USBError as e:
        err = getattr(e, "errno", None)
        if err in (110, 10060) or "timeout" in str(e).lower():
            return None
        raise
