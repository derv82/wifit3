"""RTL8821AU RX path: bulk-IN endpoint probe + rx_pkt_desc parsing.

Port of the receive-side logic from `rtw_usb_parse` (usb.c:238) and
`rtw_rx_query_rx_desc` (rx.c:264) for the 8821A chip.

For 8821A (rtw_chip_info.rx_pkt_desc_sz = 24):

    rx_desc layout (LE, 24 bytes = 6 u32 words):
        w0:  pkt_len[13:0]
             crc_err[14]
             icv_err[15]
             drv_info_sz[19:16]   (in 8-byte units)
             enc_type[23:20]
             shift[25:24]         (alignment padding in bytes)
             phy_status[26]
             swdec[27]
        w1:  macid (cam_id) bits
        w2:  c2h[28] (firmware event flag), ppdu_cnt
        w3:  rx_rate
        w4:  bandwidth
        w5:  tsf_low

Frame layout in a single bulk-IN buffer (multiple frames may be packed):

    [rx_desc (24B)] [phy_status (drv_info_sz B)] [shift padding] [MPDU (pkt_len B)]

The next frame in the same buffer starts at `round_up(24 + drv_info_sz +
shift + pkt_len, 8)`.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Iterator

import usb.core
import usb.util

logger = logging.getLogger(__name__)


RX_PKT_DESC_SZ = 24      # 8821A chip param
PHY_STATUS_BYTES = 8     # rtw_jaguar_phy_status_rpt is 8x u32 = 32B but
                         # drv_info_sz returns multiples of 8B and a Jaguar
                         # status frame is reported as drv_info_sz=4 → 32B.
DESC_RATE11M = 0x03      # last CCK rate; anything > this is OFDM/HT/VHT


@dataclass(frozen=True)
class Endpoints:
    bulk_in: list[int]   # endpoint numbers (1..15 etc.)
    bulk_out: list[int]
    interrupt: list[int]

    @property
    def primary_bulk_in(self) -> int:
        if not self.bulk_in:
            raise RuntimeError("no bulk-IN endpoint found")
        return self.bulk_in[0]


def probe_endpoints(dev: usb.core.Device, *, configuration: int = 0,
                    interface: int = 0) -> Endpoints:
    """Mirrors rtw_usb_parse — enumerate the descriptor and classify pipes."""
    cfg = dev.get_active_configuration()
    intf = cfg[(interface, 0)]
    bulk_in, bulk_out, interrupt = [], [], []
    for ep in intf:
        addr = ep.bEndpointAddress
        is_in = bool(addr & 0x80)
        ep_num = addr & 0x0F
        attr = ep.bmAttributes & 0x03
        if attr == 0x02:  # bulk
            (bulk_in if is_in else bulk_out).append(ep_num | (0x80 if is_in else 0))
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
    """Decode the 24-byte rx_pkt_desc at `buf[offset:offset+24]`.

    Raises ValueError if the buffer is short.
    """
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
        rate=w3 & 0xFF,           # RTW_RX_DESC_W3_RX_RATE
        bw=(w4 >> 4) & 0x7,       # rough — exact mask varies; debug only
        tsf_low=w5,
        macid=w1 & 0x1F,
        ppdu_cnt=(w2 >> 29) & 0x3,
    )


def _rtw8821a_cck_rx_pwr(lna_idx: int, vga_idx: int) -> int:
    """Port of rtw8821a_cck_rx_pwr (rtw8821a.c:19-39). Returns dBm.

    Note: lna_idx=3 is intentionally absent from the kernel switch — it
    falls through to default and returns 0.
    """
    lna_gain_table = (15, -1, -17, 0, -30, -38)
    if lna_idx in (0, 1, 2, 4, 5):
        return lna_gain_table[lna_idx] - 2 * vga_idx
    return 0


def parse_jaguar_phy_status_rssi(
    buf: bytes, offset: int, stat: RxPktStat,
) -> int | None:
    """Port of rtw88xxa_query_phy_status (rtw88xxa.c:1518) for the 8821A path.

    Rate-aware (mirrors the kernel branch on `pkt_stat->rate`):
      - CCK (rate <= DESC_RATE11M): lna_idx/vga_idx in w1 → 8821a CCK table.
      - OFDM/HT/VHT: gain_a in w0[6:0] → dBm = gain_a - 110.
    8821A is 1T1R, so we only read path A (no path-B max-reduction).
    Returns None only if the buffer is too short.
    """
    if len(buf) - offset < 8:
        return None
    w0, w1 = struct.unpack_from("<2I", buf, offset)

    if stat.rate <= DESC_RATE11M:
        vga_idx = (w1 >> 8) & 0x1F     # RTW_JGRPHY_W1_AGC_RPT_VGA_IDX
        lna_idx = (w1 >> 13) & 0x07    # RTW_JGRPHY_W1_AGC_RPT_LNA_IDX
        return _rtw8821a_cck_rx_pwr(lna_idx, vga_idx)

    gain_a = w0 & 0x7F                 # RTW_JGRPHY_W0_GAIN_A
    return gain_a - 110


def iter_bulk_frames(buf: bytes) -> Iterator[tuple[RxPktStat, bytes, int | None]]:
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
            # End of useful data in the buffer.
            return

        if pos + stat.total_size > len(buf):
            return

        rssi = None
        if stat.phy_status_present and stat.drv_info_sz >= 8:
            rssi = parse_jaguar_phy_status_rssi(buf, pos + RX_PKT_DESC_SZ, stat)

        mpdu_start = pos + stat.mpdu_offset
        # rtw88 USB hardware reports pkt_len inclusive of the trailing 4-byte
        # 802.11 FCS — strip it so consumers see the on-air MPDU body only
        # (see chips/rtw88_base/rx_common.py for the matching strip + the
        # FCSDIAG CRC tally that confirmed pkt_len is FCS-inclusive). This
        # driver doesn't go through rx_common, so the strip lands here too.
        mpdu = bytes(buf[mpdu_start: mpdu_start + max(stat.pkt_len - 4, 0)])

        if not stat.is_c2h:
            yield (stat, mpdu, rssi)

        # Next frame: 8-byte align
        next_pos = (pos + stat.total_size + 7) & ~7
        if next_pos <= pos:
            return
        pos = next_pos


def read_rx_burst(dev: usb.core.Device, ep: int, *,
                  max_size: int = 16384, timeout_ms: int = 100) -> bytes | None:
    """Single bulk-IN read. Returns None on timeout, bytes on success.

    PyUSB raises `usb.core.USBError` (errno=110 ETIMEDOUT) on timeout;
    we translate that to None so callers can poll without try/except.
    """
    try:
        data = dev.read(ep, max_size, timeout_ms)
        return bytes(data)
    except usb.core.USBError as e:
        # Windows libusb reports timeout as ETIMEDOUT (10060) or no_device (5).
        err = getattr(e, "errno", None)
        if err in (110, 10060) or "timeout" in str(e).lower():
            return None
        raise
