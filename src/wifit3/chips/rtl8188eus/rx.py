"""RTL8188EUS RX path — bulk-IN endpoint probe + rxdesc16 + phy_stats.

Cleanroom port of:

* `rtl8xxxu_parse_rxdesc16` — `core.c:6261-6320` (rxdesc16 layout, multi-frame
  URB iteration with `roundup(total, 128)` alignment)
* `struct rtl8xxxu_rxdesc16` — `rtl8xxxu.h:135-200` (the 16-byte u32×4
  header field layout)
* `struct rtl8723au_phy_stats` — `rtl8xxxu.h:604-640` (32-byte phy-stats
  block — only `cck_sig_qual_ofdm_pwdb_all` at offset 6 is used here)
* OFDM RSSI formula `(pwdb >> 1) - 110` — `core.c:5658`

Frame layout in a single bulk-IN URB:

    [rxdesc16 (16B)] [phy_stats (drvinfo_sz × 8 B, typ. 32 B)] [shift (0-3B)] [MPDU (pktlen B)]

The next frame in the same URB starts at `roundup(total, 128)`. URBs may
hold any number of frames; the `pkt_cnt` field in word 2 of the first
rxdesc16 is the kernel's hint.

CCK RSSI (rates 0..3) is not ported here — kernel uses an LNA/VGA lookup
(`rtl8188e_cck_rssi`, `8188e.c:1309-1331`). For first-fire we use the
OFDM formula for all rates; CCK will read a few dB low but BSSIDs parse.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Iterator

import usb.core

from .constants import (
    DESC_RATE_LAST_CCK,
    PHY_STATS_CCK_AGC_RPT_OFFSET,
    PHY_STATS_PWDB_OFFSET,
    PHY_STATS_SZ_8188E,
    RX_FRAME_ALIGN_8188E,
    RX_PKT_DESC_SZ_8188E,
)

logger = logging.getLogger(__name__)


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


def probe_endpoints(
    dev: usb.core.Device, *, interface: int = 0
) -> Endpoints:
    """Walk the USB descriptor and classify pipes."""
    cfg = dev.get_active_configuration()
    intf = cfg[(interface, 0)]
    bulk_in: list[int] = []
    bulk_out: list[int] = []
    interrupt: list[int] = []
    for ep in intf:
        addr = ep.bEndpointAddress
        is_in = bool(addr & 0x80)
        attr = ep.bmAttributes & 0x03
        if attr == 0x02:  # bulk
            (bulk_in if is_in else bulk_out).append(addr)
        elif attr == 0x03 and is_in:  # interrupt
            interrupt.append(addr)
    logger.info(
        "endpoints: bulk_in=%s bulk_out=%s interrupt=%s",
        [f"0x{e:02x}" for e in bulk_in],
        [f"0x{e:02x}" for e in bulk_out],
        [f"0x{e:02x}" for e in interrupt],
    )
    return Endpoints(bulk_in=bulk_in, bulk_out=bulk_out, interrupt=interrupt)


@dataclass(frozen=True)
class RxDesc16:
    """Decoded rtl8xxxu rxdesc16 header (subset; only fields M5 uses)."""

    pkt_len: int            # w0[13:0]  in bytes
    crc_err: bool           # w0[14]
    icv_err: bool           # w0[15]
    drv_info_sz_bytes: int  # w0[19:16] × 8 bytes — phy_stats section size
    shift: int              # w0[25:24] alignment padding bytes between phy_stats and MPDU
    phy_stats_present: bool # w0[26]
    pkt_cnt: int            # w2[23:16] (only valid in first rxdesc of a URB)
    rxmcs: int              # w3[5:0]   rate code
    rpt_sel: int            # w3[15:14] non-zero = TX report frame (skip)

    @property
    def mpdu_offset(self) -> int:
        """Byte offset within the per-frame slice where the MPDU starts."""
        return RX_PKT_DESC_SZ_8188E + self.drv_info_sz_bytes + self.shift

    @property
    def total_size(self) -> int:
        """End-of-frame relative to the start of this rxdesc16 (pre-128B-roundup)."""
        return self.mpdu_offset + self.pkt_len


def parse_rxdesc16(buf: bytes, offset: int = 0) -> RxDesc16:
    """Decode the 16-byte rxdesc16 at `buf[offset:offset+16]`."""
    if len(buf) - offset < RX_PKT_DESC_SZ_8188E:
        raise ValueError(
            f"rxdesc16 needs {RX_PKT_DESC_SZ_8188E} bytes, got {len(buf) - offset}"
        )
    w0, w1, w2, w3 = struct.unpack_from("<4I", buf, offset)
    return RxDesc16(
        pkt_len=w0 & 0x3FFF,
        crc_err=bool(w0 & (1 << 14)),
        icv_err=bool(w0 & (1 << 15)),
        drv_info_sz_bytes=((w0 >> 16) & 0xF) * 8,
        shift=(w0 >> 24) & 0x3,
        phy_stats_present=bool(w0 & (1 << 26)),
        pkt_cnt=(w2 >> 16) & 0xFF,
        rxmcs=w3 & 0x3F,
        rpt_sel=(w3 >> 14) & 0x3,
    )


# Port of `rtl8188e_cck_rssi` (8188e.c:1309-1331). LNA-index → gain-dB
# lookup. Two tables exist in the kernel keyed on `priv->chip_cut >= 8`
# (cut I, SMIC silicon). Retail TL-WN722N v2/v3 dongles are TSMC (cut
# A-D), so we default to the TSMC table. If we ever support a SMIC
# build, route through chip_cut detection (REG_SYS_CFG bits).
_CCK_LNA_GAIN_TSMC = (29, 20, 12, 3, -6, -15, -24, -33)  # 8188e.c:1314


def _rtl8188e_cck_rssi(cck_agc_rpt: int) -> int:
    """Port of `rtl8188e_cck_rssi` (8188e.c:1316-1331), TSMC silicon path.

    The single byte at phy_stats offset 5 packs:
        bits[7:5] = LNA index  → indexes the gain table
        bits[4:0] = VGA index  → each step subtracts 2 dB
    """
    lna_idx = (cck_agc_rpt >> 5) & 0x07
    vga_idx = cck_agc_rpt & 0x1F
    return _CCK_LNA_GAIN_TSMC[lna_idx] - (2 * vga_idx)


def parse_phystats_rssi(buf: bytes, offset: int, rxmcs: int) -> int | None:
    """Port of `rtl8723au_rx_parse_phystats` (core.c:5628-5660).

    Rate-aware: CCK rates (rxmcs <= DESC_RATE_LAST_CCK = 3) route through
    the 8188e LNA/VGA lookup `rtl8188e_cck_rssi` (8188e.c:1309); OFDM and
    above use ``(pwdb_all >> 1) - 110`` (core.c:5658).

    Without rate awareness, applying the OFDM formula to CCK frames reads
    -90+ dBm on strong APs — 2.4 GHz beacons are almost all CCK 1 Mbps,
    so this hits every visible BSSID.

    Byte offsets within `struct rtl8723au_phy_stats` (rtl8xxxu.h:604):
      4  cck_sig_qual_ofdm_pwdb_all  ← OFDM branch
      5  cck_agc_rpt_ofdm_cfosho_a   ← CCK branch (LNA/VGA packed)
    """
    if len(buf) - offset < PHY_STATS_SZ_8188E:
        return None

    if rxmcs <= DESC_RATE_LAST_CCK:
        cck_agc_rpt = buf[offset + PHY_STATS_CCK_AGC_RPT_OFFSET]
        return _rtl8188e_cck_rssi(cck_agc_rpt)

    pwdb = buf[offset + PHY_STATS_PWDB_OFFSET]
    return (pwdb >> 1) - 110


def iter_bulk_frames(
    buf: bytes,
) -> Iterator[tuple[RxDesc16, bytes, int | None]]:
    """Yield (desc, mpdu_bytes, rssi_dbm_or_None) for each frame in `buf`.

    Frames are concatenated with `roundup(total, 128)` alignment per
    kernel `core.c:6301`. Skips C2H / TX-report frames (rpt_sel != 0).
    """
    pos = 0
    while pos + RX_PKT_DESC_SZ_8188E <= len(buf):
        try:
            desc = parse_rxdesc16(buf, pos)
        except ValueError:
            return

        if desc.pkt_len == 0 or desc.total_size == 0:
            return
        if pos + desc.total_size > len(buf):
            return

        rssi: int | None = None
        if desc.phy_stats_present and desc.drv_info_sz_bytes >= PHY_STATS_SZ_8188E:
            rssi = parse_phystats_rssi(
                buf, pos + RX_PKT_DESC_SZ_8188E, desc.rxmcs,
            )

        if desc.rpt_sel == 0:
            mpdu_start = pos + desc.mpdu_offset
            mpdu = bytes(buf[mpdu_start : mpdu_start + desc.pkt_len])
            yield (desc, mpdu, rssi)

        # Next frame: 128-byte aligned (8188e-specific; rtw88 uses 8B).
        align = RX_FRAME_ALIGN_8188E
        next_pos = (pos + desc.total_size + align - 1) & ~(align - 1)
        if next_pos <= pos:
            return
        pos = next_pos


def read_rx_burst(
    dev: usb.core.Device,
    ep: int,
    *,
    max_size: int = 16384,
    timeout_ms: int = 100,
) -> bytes | None:
    """Single bulk-IN read. Returns None on timeout, bytes on success.

    PyUSB raises ``usb.core.USBError`` (errno 110/10060) on timeout; we
    translate that to None so callers can poll without try/except.
    """
    try:
        data = dev.read(ep, max_size, timeout_ms)
        return bytes(data)
    except usb.core.USBError as e:
        err = getattr(e, "errno", None)
        if err in (110, 10060) or "timeout" in str(e).lower():
            return None
        raise
