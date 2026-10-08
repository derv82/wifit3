"""RTL8188FTV RX path — bulk-IN endpoint probe + rxdesc24 + phy_stats.

Cleanroom port of:

* `struct rtl8xxxu_rxdesc24`  — `rtl8xxxu.h:275-390` (24-byte u32×6 header)
* `rtl8xxxu_parse_rxdesc24`   — `core.c:6461-6545` (multi-frame URB walk
  with `roundup(total, 8)` alignment — NOT the 128B the 8188e uses)
* `rtl8188f_cck_rssi`         — `8188f.c:1676-1706` (LNA/VGA lookup)
* `rtl8723au_rx_parse_phystats` — `core.c:5721-5753` (OFDM pwdb formula)
* `struct rtl8723au_phy_stats` — `rtl8xxxu.h:593-647` (32-byte drvinfo)

Frame layout in a single bulk-IN completion:

    [rxdesc24 (24B)] [drvinfo (drvinfo_sz × 8 B, typ. 32B)] [shift (0-3B)] [MPDU (pktlen B)]

The next frame starts at `roundup(pktlen + drvinfo_sz*8 + shift + 24, 8)`.
Completions may carry more than one frame when RX DMA aggregation is enabled;
iter_bulk_frames walks them all.
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
    PHY_STATS_SZ_8188F,
    RX_FRAME_ALIGN_8188F,
    RX_PKT_DESC_SZ_8188F,
    RXFLTMAP1_ACK_BIT13,
    REG_RXFLTMAP1,
)

logger = logging.getLogger(__name__)


# ---- USB endpoint geometry --------------------------------------------


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


# ---- rxdesc24 (rtl8xxxu.h:275-390) -----------------------------------


@dataclass(frozen=True)
class RxDesc24:
    """Decoded rtl8xxxu rxdesc24 header (subset; only fields the driver uses)."""

    pkt_len: int            # w0[13:0]  MPDU length in bytes
    crc_err: bool           # w0[14]
    icv_err: bool           # w0[15]
    drv_info_sz_bytes: int  # w0[19:16] × 8 — phy-stats section size
    shift: int              # w0[25:24] alignment padding between phy-stats and MPDU
    phy_stats_present: bool # w0[26]
    rpt_sel: int            # w2[28]    non-zero = C2H / TX report (skip)
    rxmcs: int              # w3[6:0]   rate code
    usb_agg_pktnum: int     # w3[23:16] multi-frame count (aggregation)

    @property
    def mpdu_offset(self) -> int:
        """Byte offset from the start of this descriptor to the MPDU."""
        return RX_PKT_DESC_SZ_8188F + self.drv_info_sz_bytes + self.shift

    @property
    def total_size(self) -> int:
        """Frame size from the start of this rxdesc (pre roundup, so it never
        exceeds the bytes actually present in the URB)."""
        return self.mpdu_offset + self.pkt_len


def parse_rxdesc24(buf: bytes, offset: int = 0) -> RxDesc24:
    """Decode the 24-byte rxdesc24 at `buf[offset:offset+24]`."""
    if len(buf) - offset < RX_PKT_DESC_SZ_8188F:
        raise ValueError(
            f"rxdesc24 needs {RX_PKT_DESC_SZ_8188F} bytes, got {len(buf) - offset}"
        )
    w0, w1, w2, w3, _w4, _w5 = struct.unpack_from("<6I", buf, offset)
    return RxDesc24(
        pkt_len=w0 & 0x3FFF,
        crc_err=bool(w0 & (1 << 14)),
        icv_err=bool(w0 & (1 << 15)),
        drv_info_sz_bytes=((w0 >> 16) & 0xF) * 8,
        shift=(w0 >> 24) & 0x3,
        phy_stats_present=bool(w0 & (1 << 26)),
        rpt_sel=(w2 >> 28) & 0x01,
        rxmcs=w3 & 0x7F,
        usb_agg_pktnum=(w3 >> 16) & 0xFF,
    )


# ---- RSSI decode (8188f.c:1676-1706, core.c:5721-5753) ---------------


def _rtl8188f_cck_rssi(cck_agc_rpt: int) -> int:
    """Port of `rtl8188f_cck_rssi` (8188f.c:1676-1706).

    Single byte packed as:
        bits[7:5] = LNA index  (CCK_AGC_RPT_LNA_IDX_MASK = GENMASK(7,5))
        bits[4:0] = VGA index  (CCK_AGC_RPT_VGA_IDX_MASK = GENMASK(4,0))

    The 8188F LNA gain cases differ from the 8188E — port from 8188f.c,
    NOT from the sibling rx.py.
    """
    lna_idx = (cck_agc_rpt >> 5) & 0x07
    vga_idx = cck_agc_rpt & 0x1F

    if lna_idx == 7:
        return -100 + 2 * (27 - vga_idx) if vga_idx <= 27 else -100
    if lna_idx == 5:
        return -74 + 2 * (21 - vga_idx)
    if lna_idx == 3:
        return -60 + 2 * (20 - vga_idx)
    if lna_idx == 1:
        return -44 + 2 * (19 - vga_idx)
    return 0


def parse_phystats_rssi(buf: bytes, offset: int, rxmcs: int) -> int | None:
    """Rate-aware RSSI from the drvinfo phy-stats block.

    Port of `rtl8723au_rx_parse_phystats` (core.c:5721-5753).  Byte offsets
    within `struct rtl8723au_phy_stats` (rtl8xxxu.h:593):

      4  cck_sig_qual_ofdm_pwdb_all  ← OFDM branch
      5  cck_agc_rpt_ofdm_cfosho_a   ← CCK branch (LNA/VGA packed)

    Without rate awareness, applying the OFDM formula to CCK frames reads
    -90+ dBm on strong APs — 2.4 GHz beacons are almost all CCK 1 Mbps,
    so this hits every visible BSSID.
    """
    if len(buf) - offset < PHY_STATS_SZ_8188F:
        return None

    if rxmcs <= DESC_RATE_LAST_CCK:
        cck_agc_rpt = buf[offset + PHY_STATS_CCK_AGC_RPT_OFFSET]
        return _rtl8188f_cck_rssi(cck_agc_rpt)

    pwdb = buf[offset + PHY_STATS_PWDB_OFFSET]
    return (pwdb >> 1) - 110


# ---- bulk frame iterator ----------------------------------------------


def iter_bulk_frames(
    buf: bytes,
) -> Iterator[tuple[RxDesc24, bytes, int | None]]:
    """Yield (desc, mpdu_bytes, rssi_dbm_or_None) for each frame in `buf`.

    Frames are walked with `roundup(total, 8)` alignment
    (core.c:6494).  Skips C2H / TX-report frames (rpt_sel != 0).
    """
    pos = 0
    while pos + RX_PKT_DESC_SZ_8188F <= len(buf):
        try:
            desc = parse_rxdesc24(buf, pos)
        except ValueError:
            return

        if desc.pkt_len == 0 or desc.total_size == 0:
            return
        if pos + desc.total_size > len(buf):
            return

        rssi: int | None = None
        if desc.phy_stats_present and desc.drv_info_sz_bytes >= PHY_STATS_SZ_8188F:
            rssi = parse_phystats_rssi(
                buf, pos + RX_PKT_DESC_SZ_8188F, desc.rxmcs,
            )

        # Drop HW-flagged corrupt frames (belt-and-suspenders, matches the
        # 8188eus convention).  The monitor RCR already HW-filters most CRC
        # failures; this guards against the remainder leaking false beacons
        # into the AP registry.
        if desc.rpt_sel == 0 and not (desc.crc_err or desc.icv_err):
            mpdu_start = pos + desc.mpdu_offset
            mpdu = bytes(buf[mpdu_start : mpdu_start + desc.pkt_len])
            yield (desc, mpdu, rssi)

        next_pos = (pos + desc.total_size + RX_FRAME_ALIGN_8188F - 1) & ~(RX_FRAME_ALIGN_8188F - 1)
        if next_pos <= pos:
            return
        pos = next_pos


# ---- USB bulk read ----------------------------------------------------


def read_rx_burst(
    dev: usb.core.Device,
    ep: int,
    *,
    max_size: int = 16384,
    timeout_ms: int = 100,
) -> bytes | None:
    """Single bulk-IN read.  Returns None on timeout, bytes on success.

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


# ---- ACK filter (RXFLTMAP1 bit13) -------------------------------------


def admit_ack_frames(transport) -> None:
    """Set RXFLTMAP1 bit 13 to admit ACK control frames.

    The kernel's `init_reg_rxfltmap` writes RXFLTMAP1 = 0x0400 (PS-Poll only,
    core.c:4247).  Subtype 13 = ACK (regs.h:859-860); this opens the filter
    so the RX tap can observe ACKs to our injects.
    """
    val = transport.read16(REG_RXFLTMAP1)
    transport.write16(REG_RXFLTMAP1, val | RXFLTMAP1_ACK_BIT13)


def drop_ack_frames(transport) -> None:
    """Clear RXFLTMAP1 bit 13 to restore the default monitor filter."""
    val = transport.read16(REG_RXFLTMAP1)
    transport.write16(REG_RXFLTMAP1, val & ~RXFLTMAP1_ACK_BIT13)
