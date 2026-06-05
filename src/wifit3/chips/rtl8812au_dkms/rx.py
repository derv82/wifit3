"""RTL8812AU RX — the per-chip PHY-status RSSI formula over the shared base desc walk.

The 24-byte RX-status descriptor decode + aggregated-buffer walk live in
``rtl88xxau_base.rx``; this module supplies the 8812a RSSI. CCK uses
``phydm_cck_rssi_8812a`` [SRC] phydm_rtl8812a.c — 8 LNA levels (0-7), distinct from the
8821a's 5. OFDM keeps the jaguar ``((pwdb_all >> 1) & 0x7f) - 110``: pwdb_all is the AGC
sum across the DC paths (two on this 2T2R part), halved before the dBm conversion — the
``>>1`` is mandatory or 5 GHz OFDM beacons read ~2x too strong and saturate.
"""
from __future__ import annotations

from typing import Iterator, Tuple

from ..rtl88xxau_base.rx import RSSI_UNKNOWN, iter_frames as _base_iter_frames


def _cck_rssi_8812a(lna_idx: int, vga_idx: int) -> int:
    """[SRC] phydm_cck_rssi_8812a (phydm_rtl8812a.c) — CCK signal power (dBm)."""
    if lna_idx == 7:
        return (-94 + 2 * (27 - vga_idx)) if vga_idx <= 27 else -94
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


def decode_rssi(phy_status: bytes, data_rate: int) -> int:
    """Per-frame RSSI in dBm from the jaguar PHY-status struct.

    [SRC] phydm_rx_phy_status_jaguar_series_parsing: CCK (rate<=3) reads the CCK AGC
    report (byte 5) -> lna[7:5]/vga[4:0] -> phydm_cck_rssi_8812a; OFDM reads pwdb_all
    (byte 4) as ``((pwdb_all >> 1) & 0x7f) - 110``.
    """
    if len(phy_status) < 6:
        return RSSI_UNKNOWN
    if data_rate <= 3:                              # CCK (1/2/5.5/11 Mbps)
        cck_agc_rpt = phy_status[5]
        return _cck_rssi_8812a((cck_agc_rpt & 0xE0) >> 5, cck_agc_rpt & 0x1F)
    return ((phy_status[4] >> 1) & 0x7F) - 110      # OFDM/HT/VHT pwdb_all (>>1 per phydm)


def iter_frames(buf: bytes) -> Iterator[Tuple[bytes, int]]:
    """Walk the aggregated bulk-IN buffer, yielding (frame, rssi) with the 8812a RSSI."""
    return _base_iter_frames(buf, decode_rssi)
