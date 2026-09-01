"""RTL8822CU RX stream decoding.

RTL8822C uses the rtw88 RX descriptor layout (24 bytes). The PHY status report is
JGR3: type 0 is CCK, types 1..5 share one OFDM PWDB layout.
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


def _s8(b: int) -> int:
    return b - 256 if b > 127 else b


def _cck_path_power(pwdb: int, gain_byte: int, l_bnd: int | None, u_bnd: int | None) -> int:
    """One CCK RX path's pwdb (pre -110), gain-corrected against the cck_gi bounds
    [SRC phydm_get_physts_0_jgr3, phydm_phystatus.c:1993-2011]. gain is the low 6 bits of
    gain_byte. With no bounds the raw (s8) pwdb is returned (the correction is skipped)."""
    rx_power = _s8(pwdb)
    if l_bnd is not None:
        gain = gain_byte & 0x3F
        if gain < l_bnd:
            rx_power += (l_bnd - gain) << 1
        elif gain > u_bnd:
            rx_power -= (gain - u_bnd) << 1
    return rx_power


def _phy_rssi(buf: bytes, offset: int, stat: RxPktStat,
              cck_gi_l_bnd: int | None = None, cck_gi_u_bnd: int | None = None) -> int | None:
    # JGR3 PHY status: dispatch on the low type nibble (phydm_rx_physts_jgr3,
    # phydm_phystatus.c:2435). pwdb = (s8)agc_byte - 110, floor -120, NO upper
    # clamp (recv_signal_power = rx_pwr_db_max, init -120, at c:2050 / c:2312).
    if len(buf) - offset < 3:
        return None
    phy_type = buf[offset] & 0xF
    if phy_type == 0:
        # CCK: gain-correct each RX path's pwdb, take the max [SRC c:1988-2052]. Path A
        # pwdb/gain at offset+1/+2; path B at offset+16/+19 when the 32-byte report is present.
        rx_power = _cck_path_power(buf[offset + 1], buf[offset + 2], cck_gi_l_bnd, cck_gi_u_bnd)
        if stat is not None and stat.drv_info_sz >= 20 and len(buf) - offset >= 20:
            rx_power = max(rx_power, _cck_path_power(buf[offset + 16], buf[offset + 19],
                                                     cck_gi_l_bnd, cck_gi_u_bnd))
        return max(-120, rx_power - 110)
    if 1 <= phy_type <= 5:
        # OFDM types 1..5 share one pwdb[] layout; 2 RX paths at offset+1..+2
        # (phydm_get_physts_ofdm_cmn_jgr3, c:2306).
        return max(-120, max(_s8(buf[offset + 1]), _s8(buf[offset + 2])) - 110)
    return None


def iter_bulk_frames(buf: bytes, *, cck_gi_l_bnd: int | None = None,
                     cck_gi_u_bnd: int | None = None):
    def phy_status_rssi(b: bytes, off: int, stat: RxPktStat) -> int | None:
        return _phy_rssi(b, off, stat, cck_gi_l_bnd, cck_gi_u_bnd)
    return _iter_bulk_frames(buf, phy_status_rssi=phy_status_rssi)


__all__ = ["Endpoints", "RxPktStat", "iter_bulk_frames", "parse_rx_pkt_desc", "probe_endpoints", "read_rx_burst"]
