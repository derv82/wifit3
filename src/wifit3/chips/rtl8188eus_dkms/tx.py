"""RTL8188EUS TX descriptor builder + checksum (management inject).

Ports the **management-frame branch** of ``update_txdesc`` [SRC] usb/rtl8188eu_xmit.c:445
(``MGNT_FRAMETAG``) — the descriptor the vendor driver actually builds for an injected
mgmt frame (what aireplay-ng's monitor inject rides), byte-diffed against the cold-boot
capture's deauth + probe-request descriptors. It is NOT ``fill_fake_txdesc`` (that builds
the null/reserved-page frame); the two share txdw0/3/4 but the injected-mgmt one also stamps
MACID + RAID (txdw1) and the retry-limit (txdw5), which the fake one leaves zero.

Field set for a monitor-injected mgmt frame at the HW-default rate (DESC_RATE1M, no rate
adaptation), HW-assigned sequence number, no HW encryption:
  txdw0  OWN|FSG|LSG, OFFSET=TXDESC_SIZE, PKT_SIZE  (+ BMC if addr1 is group-addressed)
  txdw1  MACID(1) | QSEL=QSLT_MGNT | RAID(6)        [WIRE: mac_id 1, raid 6 — see constants]
  txdw3  EN_HWSEQ (8<<28)                           (Hw assigns the sequence number)
  txdw4  USERATE (driver picks rate -> 1M) | HW_SSN
  txdw5  RTY_LMT_EN | retry-limit 12 | MRateToHwRate(1M)=0
  txdw7  checksum (computed last)

The 32-byte descriptor and the XOR checksum (``rtl8188e_cal_txdesc_chksum``: XOR of the
16 little-endian u16 with the checksum field txdw7[15:0] zeroed first) are 8188e-specific.
``inject_frame`` prepends ``[desc | frame]`` and sends it on the bulk-OUT pipe.

NOTE: TX is the only human-gated step — this builds + sends, but live 802.11 TX
(deauth / replay) is fired by the user, not the agent.
"""
from __future__ import annotations

from .constants import (
    BMC,
    DATA_RETRY_LIMIT_12,
    FSG,
    LSG,
    MGMT_INJECT_MACID,
    MGMT_INJECT_RAID,
    OFFSET_SHT,
    OFFSET_SZ,
    OWN,
    QSEL_SHT,
    QSLT_MGNT,
    RATE_ID_SHT,
    RTY_LMT_EN,
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


def build_mgmt_txdesc(pkt_len: int, *, bmc: bool = False, seqnum: int = 0) -> bytes:
    """Build the 32-byte management TX descriptor for one injected frame [SRC] update_txdesc
    MGNT_FRAMETAG branch (rtl8188eu_xmit.c:445). ``bmc`` sets the broadcast/multicast bit when
    addr1 is a group address (e.g. a broadcast deauth). ``seqnum`` is the frame's 802.11
    sequence number, which the driver copies into txdw3 (the wire confirms desc-seq ==
    frame-seqctrl>>4 across every injected frame). No SEC_TYPE — the frame is already final
    (no HW encryption). Rate is the driver default DESC_RATE1M (MRateToHwRate=0)."""
    d = bytearray(TXDESC_SIZE)
    dw0 = (OWN | FSG | LSG
           | (((TXDESC_SIZE + OFFSET_SZ) << OFFSET_SHT) & 0x00FF0000)
           | (pkt_len & 0x0000FFFF))
    if bmc:
        dw0 |= BMC
    _put32(d, 0, dw0)
    _put32(d, 4, (MGMT_INJECT_MACID & 0x3F)                  # txdw1: MACID
           | ((QSLT_MGNT << QSEL_SHT) & 0x00001F00)          #        QSEL = MGMT queue
           | ((MGMT_INJECT_RAID << RATE_ID_SHT) & 0x000F0000))  #      RAID
    _put32(d, 12, (8 << 28) | ((seqnum << 16) & 0x0FFF0000))  # txdw3: EN_HWSEQ | seq number
    _put32(d, 16, (1 << 7) | (1 << 8))                       # txdw4: HW_SSN | USERATE
    _put32(d, 20, RTY_LMT_EN | DATA_RETRY_LIMIT_12)          # txdw5: retry-limit en + 12, rate 0
    _put32(d, 28, txdesc_checksum(d))                        # txdw7: checksum (computed last)
    return bytes(d)
