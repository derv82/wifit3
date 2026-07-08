"""RTL8822BU RX — 24-byte rx_pkt_desc decode + aggregated bulk-IN buffer walk.

Ports rtl8822bu_recv.c (recvbuf2recvframe) + rtl8822b_rxdesc2attribute `[SRC] rtl8822b_ops.c:3859`
with the HALMAC NIC rx-desc bitfields `[SRC] halmac_rx_desc_nic.h`. The bulk-IN buffer is a run of
8-byte-aligned packets, each `[24B rxdesc | drvinfo (PHY status) | shift | MPDU]`. The MPDU starts
at `RXDESC_SIZE + drvinfo_sz + shift_sz`; the next packet is at `_RND8(that + pkt_len)`.

We deliver only good NORMAL_RX frames (skip C2H reports and CRC/ICV-error packets, per the vendor
walk). The monitor RCR sets APPFCS, so pkt_len includes the trailing 4-byte FCS; we drop it so the
delivered frame ends at the MPDU end — the FCS-stripped convention every other wifit3 driver
follows. RSSI from the PHY-status report is a coarse pwdb for now (precise jaguar2 parsing is a
later refinement; beacon detection does not need it).
"""
from __future__ import annotations

from typing import Iterator, Tuple

RXDESC_SIZE = 24
FCS_LEN = 4
RSSI_UNKNOWN = -128
RSSI_FLOOR = -110       # a frame we received but couldn't measure the power of: the pwdb noise floor


def _rnd8(x: int) -> int:
    return (x + 7) & ~7


def _s8(v: int) -> int:
    """C `s8` cast (the vendor stores rx_pow in an s8, so pwdb-110 wraps at the byte boundary)."""
    v &= 0xFF
    return v - 256 if v >= 128 else v


def _path_rssi(pwdb: int) -> int | None:
    """One RX path's `pwdb` byte -> dBm (`pwdb - 110` as the vendor's `s8 rx_pow`), or None when the
    byte is not a real measurement. `pwdb` is a 0..~95 power index; the HW emits a high byte for a
    failed AGC read on a marginal frame. `>=0x80` wraps via s8 to the noise floor (-110..-1) and is
    kept (vendor-faithful — a saturating 0xfd reads -113, not +143), but `pwdb` in 111..145 yields an
    impossible >0 dBm — a garbage reading, so we reject it rather than let it poison the per-AP mean
    (HW-observed: weak adjacent-channel bleed frames report pwdb~122-130 -> +12/+20 dBm)."""
    rssi = _s8(pwdb - 110)
    return None if rssi > 0 else rssi


def _decode_rssi(phy_status: bytes) -> int:
    """RSSI (dBm) from the 8822b jaguar2 PHY-status report `[SRC] phydm_get_phy_sts_type0/1`.

    The report is a `phy_sts_rpt_jgr2_type{0,1,2}`; `page = byte0[3:0]` selects it (0 = CCK / type0,
    1·2 = OFDM / type1·2). For CCK, 8822b runs new-CCK-AGC (`0xA9C[17]=1`, HW-confirmed), so the RSSI
    is `pwdb(byte1) - 110` — the `!cck_new_agc` lna/vga-table path (`phydm_get_cck_rssi`) is dead on
    this card (`cck_lna_gain_table` is 8197F-only). For OFDM, each path's `pwdb[i]` is `byte[1+i]`;
    RSSI is the strongest *valid* active-path power (paths A·B on this 2T2R card, `rx_ant_status=0x3`).
    Per-path garbage bytes are dropped (`_path_rssi`); if no path is valid we fall back to the floor,
    since the frame did pass CRC. The vendor hides these garbage per-frame reads via per-STA IIR
    smoothing (`phydm_process_rssi_for_dm`); we report per-frame, so we filter instead."""
    if len(phy_status) < 2:
        return RSSI_UNKNOWN
    page = phy_status[0] & 0xF
    if page == 0:                                              # type0 (CCK), new-CCK-AGC
        r = _path_rssi(phy_status[1])
        return r if r is not None else RSSI_FLOOR
    if page in (1, 2) and len(phy_status) >= 3:                # type1/2 (OFDM): strongest valid path
        cand = [r for r in (_path_rssi(phy_status[1]), _path_rssi(phy_status[2])) if r is not None]
        return max(cand) if cand else RSSI_FLOOR               # active paths A, B (2T2R)
    return RSSI_UNKNOWN


def iter_frames(buf: bytes) -> Iterator[Tuple[bytes, int]]:
    """Walk the aggregated bulk-IN buffer, yielding (mpdu, rssi) for each good NORMAL_RX frame."""
    off, n = 0, len(buf)
    while off + RXDESC_SIZE <= n:
        w0 = int.from_bytes(buf[off:off + 4], "little")
        pkt_len = w0 & 0x3FFF
        crc_err = (w0 >> 14) & 1
        icv_err = (w0 >> 15) & 1
        drvinfo_sz = ((w0 >> 16) & 0xF) << 3
        shift_sz = (w0 >> 24) & 0x3
        physt = (w0 >> 26) & 1
        c2h = (int.from_bytes(buf[off + 8:off + 12], "little") >> 28) & 1

        if pkt_len <= 0:
            break
        pkt_offset = RXDESC_SIZE + drvinfo_sz + shift_sz + pkt_len
        if pkt_offset > n - off:                         # truncated tail packet
            break
        if not c2h and not crc_err and not icv_err and pkt_len > FCS_LEN:
            start = off + RXDESC_SIZE + drvinfo_sz + shift_sz
            mpdu = buf[start:start + pkt_len - FCS_LEN]      # drop the appended FCS
            rssi = _decode_rssi(buf[off + RXDESC_SIZE:start]) if physt else RSSI_UNKNOWN
            yield mpdu, rssi
        off += _rnd8(pkt_offset)
