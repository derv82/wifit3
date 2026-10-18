"""RTL8192EUS RX path — bulk-IN endpoint probe + rxdesc24 + phy_stats.

Cleanroom port of:

* `rtl8xxxu_parse_rxdesc24` — `core.c:6392-6496` (the 8192eu `.parse_rx_desc`,
  `8192e.c:1735`; rxdesc24 layout, multi-frame URB iteration with
  `roundup(total, 8)` alignment)
* `struct rtl8xxxu_rxdesc24` — `rtl8xxxu.h:275-398` (the 24-byte descriptor:
  five u32-bitfield words + trailing `u32 tsfl`; `.rx_desc_size` at
  `8192e.c:1750`)
* `rtl8723au_rx_parse_phystats` — `core.c:5640-5672` (rate-aware RSSI; the
  8192eu fops selects this shared parser at `8192e.c:1736`)
* `rtl8192e_cck_rssi` — `8192e.c:1675-1696` (the CCK branch that parser
  dispatches to via `fops->cck_rssi`, `8192e.c:1746`)
* `struct rtl8723au_phy_stats` — `rtl8xxxu.h:604-640` (32-byte phy-stats block
  appended when `REG_RX_DRVINFO_SZ = 4`, i.e. `RCR_APPEND_PHYSTAT`)
* OFDM RSSI formula `(pwdb >> 1) - 110` — `core.c:5669-5670`

Frame layout in a single bulk-IN URB:

    [rxdesc24 (24B)] [phy_stats (drvinfo_sz × 8 B, typ. 32 B)] [shift (0-3B)] [MPDU (pktlen B)]

The next frame in the same URB starts at `roundup(total, 8)` (`core.c:6423-6424`).
URBs may hold any number of frames; the iteration stops once fewer than one
descriptor's worth of bytes remains (`core.c:6493`).
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Iterator

import usb.core

from .constants import (
    CCK_AGC_RPT_LNA_IDX_MASK,
    CCK_AGC_RPT_VGA_IDX_MASK,
    DESC_RATE_LAST_CCK,
    PHY_STATS_CCK_AGC_RPT_OFFSET,
    PHY_STATS_PWDB_OFFSET,
    PHY_STATS_SZ_8188E as PHY_STATS_BLOCK_SZ,
)

logger = logging.getLogger(__name__)

# `PHY_STATS_BLOCK_SZ` is `sizeof(struct rtl8723au_phy_stats)`
# (rtl8xxxu.h:604-640), the drvinfo block the 8192eu's `.parse_phystats`
# reads (8192e.c:1736); constants.py keeps the legacy `_8188E` symbol name.

# `sizeof(struct rtl8xxxu_rxdesc24)`: five u32 little-endian bitfield words
# (rtl8xxxu.h:276-337) plus the trailing `u32 tsfl` (rtl8xxxu.h:397) = 24 B.
# This is the 8192eu fileops' `.rx_desc_size` (8192e.c:1750).
RX_PKT_DESC_SZ_24 = 24
# Inter-frame stride inside a bulk-IN URB: `roundup(..., 8)` (core.c:6423-6424).
# NOT the 128 the rxdesc16 path uses.
RX_FRAME_ALIGN_24 = 8


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
    """Walk the USB descriptor and classify pipes.

    Classification mirrors `rtl8xxxu_parse_usb` (core.c:7582-7636): bulk-IN,
    bulk-OUT and interrupt-IN endpoints are pulled from interface 0.
    """
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
class RxDesc24:
    """Decoded rtl8xxxu rxdesc24 header (the fields this RX path reads)."""

    pkt_len: int            # w0[13:0]  in bytes
    crc_err: bool           # w0[14]    crc32
    icv_err: bool           # w0[15]    icverr
    drv_info_sz_bytes: int  # w0[19:16] × 8 bytes — phy_stats section size
    shift: int              # w0[25:24] alignment padding bytes between phy_stats and MPDU
    phy_stats_present: bool # w0[26]    phy_stats
    rpt_sel: int            # w2[28]    1 bit: non-zero = C2H/TX-report frame (skip)
    usb_agg_pktnum: int     # w3[23:16] packets in this USB aggregate (informational)
    rxmcs: int              # w3[6:0]   7-bit rate code

    @property
    def mpdu_offset(self) -> int:
        """Byte offset within the per-frame slice where the MPDU starts."""
        return RX_PKT_DESC_SZ_24 + self.drv_info_sz_bytes + self.shift

    @property
    def total_size(self) -> int:
        """End-of-frame relative to the start of this rxdesc24 (pre-8B-roundup)."""
        return self.mpdu_offset + self.pkt_len


def parse_rxdesc24(buf: bytes, offset: int = 0) -> RxDesc24:
    """Decode the 24-byte rxdesc24 at `buf[offset:offset+24]`."""
    if len(buf) - offset < RX_PKT_DESC_SZ_24:
        raise ValueError(
            f"rxdesc24 needs {RX_PKT_DESC_SZ_24} bytes, got {len(buf) - offset}"
        )
    w0, _w1, w2, w3 = struct.unpack_from("<4I", buf, offset)
    return RxDesc24(
        pkt_len=w0 & 0x3FFF,
        crc_err=bool(w0 & (1 << 14)),
        icv_err=bool(w0 & (1 << 15)),
        drv_info_sz_bytes=((w0 >> 16) & 0xF) * 8,
        shift=(w0 >> 24) & 0x3,
        phy_stats_present=bool(w0 & (1 << 26)),
        rpt_sel=(w2 >> 28) & 0x1,
        usb_agg_pktnum=(w3 >> 16) & 0xFF,
        rxmcs=w3 & 0x7F,
    )


# Port of `rtl8192e_cck_rssi` (8192e.c:1675-1696). Two LNA-index → gain-dB
# tables selected by `priv->cck_agc_report_type`, which `rtl8xxxu_gen2_usb_quirks`
# reads from REG_FPGA0_XA_HSSI_PARM2 bit 9 (core.c:4348-4350, regs.h:912).
# type 0 → table_0, else table_1. (The 8188e's table is keyed on chip_cut
# instead — 8188e.c:1324 — so this is NOT the same table.)
_CCK_LNA_GAIN_TABLE_0 = (15, 9, -10, -21, -23, -27, -43, -44)   # 8192e.c:1677
_CCK_LNA_GAIN_TABLE_1 = (24, 18, 13, -4, -11, -18, -31, -36)    # 8192e.c:1678


def _rtl8192e_cck_rssi(cck_agc_rpt: int, cck_agc_report_type: int) -> int:
    """Port of `rtl8192e_cck_rssi` (8192e.c:1675-1696).

    The single byte at phy_stats offset 5 packs:
        bits[7:5] = LNA index  → indexes the report-type-selected gain table
        bits[4:0] = VGA index  → each step subtracts 2 dB
    """
    lna_idx = (cck_agc_rpt & CCK_AGC_RPT_LNA_IDX_MASK) >> 5  # GENMASK(7,5) → ffs=5
    vga_idx = cck_agc_rpt & CCK_AGC_RPT_VGA_IDX_MASK
    table = _CCK_LNA_GAIN_TABLE_0 if cck_agc_report_type == 0 else _CCK_LNA_GAIN_TABLE_1
    return table[lna_idx] - (2 * vga_idx)


def parse_phystats_rssi(
    buf: bytes,
    offset: int,
    rxmcs: int,
    *,
    cck_agc_report_type: int = 0,
) -> int | None:
    """Port of `rtl8723au_rx_parse_phystats` (core.c:5640-5672).

    Rate-aware: CCK rates (rxmcs < DESC_RATE_6M == 0x04, i.e. <=
    DESC_RATE_LAST_CCK) route through the 8192eu LNA/VGA lookup
    `rtl8192e_cck_rssi` (8192e.c:1675, wired at `8192e.c:1746`); OFDM and above
    use ``(pwdb_all >> 1) - 110`` (core.c:5669-5670).

    `cck_agc_report_type` is `priv->cck_agc_report_type` (core.c:4349) and picks
    the CCK LNA table; it defaults to the kernel's zero-initialised value.

    Byte offsets within `struct rtl8723au_phy_stats` (rtl8xxxu.h:604):
      4  cck_sig_qual_ofdm_pwdb_all  ← OFDM branch
      5  cck_agc_rpt_ofdm_cfosho_a   ← CCK branch (LNA/VGA packed)
    """
    if len(buf) - offset < PHY_STATS_BLOCK_SZ:
        return None

    if rxmcs <= DESC_RATE_LAST_CCK:
        cck_agc_rpt = buf[offset + PHY_STATS_CCK_AGC_RPT_OFFSET]
        return _rtl8192e_cck_rssi(cck_agc_rpt, cck_agc_report_type)

    pwdb = buf[offset + PHY_STATS_PWDB_OFFSET]
    return (pwdb >> 1) - 110


def iter_bulk_frames(
    buf: bytes,
    *,
    cck_agc_report_type: int = 0,
) -> Iterator[tuple[RxDesc24, bytes, int | None]]:
    """Yield (desc, mpdu_bytes, rssi_dbm_or_None) for each frame in `buf`.

    Frames are concatenated with `roundup(total, 8)` alignment per kernel
    `core.c:6423-6424`. C2H / TX-report frames (rpt_sel != 0) are dropped: the
    kernel routes them to `rtl8723bu_handle_c2h` (core.c:6447), which this
    monitor-only port does not implement.
    """
    pos = 0
    while pos + RX_PKT_DESC_SZ_24 <= len(buf):
        try:
            desc = parse_rxdesc24(buf, pos)
        except ValueError:
            return

        if pos + desc.total_size > len(buf):
            return

        rssi: int | None = None
        if desc.phy_stats_present and desc.drv_info_sz_bytes >= PHY_STATS_BLOCK_SZ:
            rssi = parse_phystats_rssi(
                buf, pos + RX_PKT_DESC_SZ_24, desc.rxmcs,
                cck_agc_report_type=cck_agc_report_type,
            )

        # Drop HW-flagged corrupt frames (belt-and-suspenders). The monitor RCR
        # already HW-filters CRC/ICV-failed frames, so this rarely fires — it guards
        # against an RCR that accepts them leaking corrupt frames that parse as
        # beacons with garbage BSSID/ESSID and inflate the AP count.
        if (
            desc.pkt_len > 0
            and desc.rpt_sel == 0
            and not (desc.crc_err or desc.icv_err)
        ):
            mpdu_start = pos + desc.mpdu_offset
            mpdu = bytes(buf[mpdu_start : mpdu_start + desc.pkt_len])
            yield (desc, mpdu, rssi)

        # Next frame: 8-byte aligned (core.c:6423-6424).
        align = RX_FRAME_ALIGN_24
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
