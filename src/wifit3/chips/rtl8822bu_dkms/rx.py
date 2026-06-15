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


def _rnd8(x: int) -> int:
    return (x + 7) & ~7


def _decode_rssi(phy_status: bytes) -> int:
    """Coarse RSSI (dBm) from the jaguar2 PHY-status report. pwdb_all lives at byte 0 of the
    rx_phy_status; the full per-path LNA/VGA parse is deferred. Returns RSSI_UNKNOWN if absent."""
    if len(phy_status) < 1:
        return RSSI_UNKNOWN
    pwdb = phy_status[0] & 0x7F
    return pwdb - 110


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
