"""RTL8188EUS TX descriptor builder + checksum (management inject).

Ports ``rtl8188e_fill_fake_txdesc`` [SRC] usb/rtl8188eu_xmit.c:72 — the vendor's minimal,
self-contained management TX descriptor (what it hands the HW to transmit a frame
directly), for the not-PsPoll / not-data-frame case. That field set is exactly what a
monitor-mode deauth / WEP replay needs: one management frame at the HW-default rate
(DESC_RATE1M, no rate adaptation), HW-assigned sequence number, no HW encryption.

The 32-byte descriptor and the XOR checksum (``rtl8188e_cal_txdesc_chksum``: XOR of the
16 little-endian u16 with the checksum field txdw7[15:0] zeroed first) are 8188e-specific.
``inject_frame`` prepends ``[desc | frame]`` and sends it on the bulk-OUT pipe.

NOTE: TX is the only human-gated step — this builds + sends, but live 802.11 TX
(deauth / replay) is fired by the user, not the agent.
"""
from __future__ import annotations

from .constants import (
    BMC,
    FSG,
    LSG,
    OFFSET_SHT,
    OFFSET_SZ,
    OWN,
    QSEL_SHT,
    QSLT_MGNT,
    TXDESC_SIZE,
)


def _put32(desc: bytearray, off: int, value: int) -> None:
    desc[off:off + 4] = (value & 0xFFFFFFFF).to_bytes(4, "little")


def txdesc_checksum(desc: bytes) -> int:
    """``rtl8188e_cal_txdesc_chksum`` — XOR of the 32-byte desc as 16 LE u16, with the
    checksum field (txdw7[15:0], byte offset 28) cleared first."""
    d = bytearray(desc)
    d[28] = d[29] = 0
    cs = 0
    for i in range(16):
        cs ^= int.from_bytes(d[2 * i:2 * i + 2], "little")
    return cs & 0xFFFF


def build_mgmt_txdesc(pkt_len: int, *, bmc: bool = False) -> bytes:
    """Build the 32-byte management TX descriptor for one frame.

    [SRC] rtl8188e_fill_fake_txdesc (not-PsPoll / not-data): txdw0 OWN|FSG|LSG, OFFSET =
    TXDESC_SIZE, PKT_SIZE; txdw1 QUEUE_SEL=QSLT_MGNT; txdw3 bit3 (8<<28, per TimChen);
    txdw4 BIT7 (HW assigns the sequence number) + BIT8 (driver uses rate -> the default
    DESC_RATE1M); then the descriptor checksum. ``bmc`` sets the broadcast/multicast bit
    when addr1 is a group address (e.g. a broadcast deauth). No SEC_TYPE — the injected
    frame is already final (no HW encryption)."""
    d = bytearray(TXDESC_SIZE)
    dw0 = (OWN | FSG | LSG
           | (((TXDESC_SIZE + OFFSET_SZ) << OFFSET_SHT) & 0x00FF0000)
           | (pkt_len & 0x0000FFFF))
    if bmc:
        dw0 |= BMC
    _put32(d, 0, dw0)
    _put32(d, 4, (QSLT_MGNT << QSEL_SHT) & 0x00001F00)   # txdw1: MGMT queue
    _put32(d, 12, 8 << 28)                               # txdw3: bit3 (TimChen)
    _put32(d, 16, (1 << 7) | (1 << 8))                   # txdw4: HW seq + driver-uses-rate
    _put32(d, 28, txdesc_checksum(d))                    # txdw7: checksum (computed last)
    return bytes(d)
