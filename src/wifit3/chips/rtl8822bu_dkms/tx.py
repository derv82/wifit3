"""RTL8822BU TX descriptor build (monitor injection).

Ports `fill_fake_txdesc` [SRC] rtl8822b_ops.c:3715 — the 48-byte "send this frame as-is" descriptor
for one non-aggregated, unencrypted mgmt/data frame at a fixed rate with a HW-assigned sequence
number (exactly what monitor injection needs; not the rate-adaptation `update_txdesc` path). The
agent never fires live TX; this only *builds* the bulk-OUT payload (descriptor + frame). There is no
TX in the passive cold-boot capture (the only TX there is aireplay-ng's, a different program), so
this path has no pcap gate — it is unit-tested against the HALMAC field offsets + the XOR-16 checksum.
"""
from __future__ import annotations

import struct

from .constants import (
    DESC_RATE1M,
    RATEID_IDX_B,
    TX_DESC_SIZE_88XX,
    TXDESC_QSEL_MGNT,
)
from .firmware import _set_le32_bits


def build_inject_txdesc(frame: bytes, *, qsel: int = TXDESC_QSEL_MGNT, macid: int = 0,
                        hw_rate: int = DESC_RATE1M, rate_id: int = RATEID_IDX_B) -> bytes:
    """48-byte fill_fake_txdesc descriptor prepended to `frame` = the bulk-OUT payload.

    [SRC] rtl8822b_ops.c fill_fake_txdesc (non-PsPoll / non-BTQosNull): LS + OFFSET=48 + TXPKTSIZE,
    QSEL=MGNT, RATE_ID, DISQSELSEQ + EN_HWSEQ (the HW assigns the sequence number), USE_RATE +
    DATARATE (fixed rate — no rate adaptation). MACID / HW_SSN_SEL / EN_HWEXSEQ / SEC_TYPE / PORT_ID /
    MULTIPLE_PORT are all 0 (left implicit by the zero-init; the frame is already final, so no HW
    re-encryption). The 8822b USB txdesc has no FS / OWN field. BMC (word0[24]) is set when addr1
    (frame[4]) is group-addressed. Field offsets [SRC] halmac_tx_desc_nic.h; XOR-16 checksum over the
    first 32 bytes [SRC] halmac_common_8822b.c fill_txdesc_check_sum_8822b (shared with the firmware.py
    builders). Rides bulk-OUT EP 0x05 (MGNT qsel -> HIGH pipe -> RtOutPipe[0])."""
    d = bytearray(TX_DESC_SIZE_88XX)
    _set_le32_bits(d, 0x00, 0, 16, len(frame))          # TXPKTSIZE  word0[0:16]
    _set_le32_bits(d, 0x00, 16, 8, TX_DESC_SIZE_88XX)   # OFFSET     word0[16:24] (desc bytes)
    if len(frame) >= 5 and (frame[4] & 0x01):
        _set_le32_bits(d, 0x00, 24, 1, 1)               # BMC        word0[24] (group-addressed RA)
    _set_le32_bits(d, 0x00, 26, 1, 1)                   # LS         word0[26] (last segment)
    _set_le32_bits(d, 0x00, 31, 1, 1)                   # DISQSELSEQ word0[31]
    _set_le32_bits(d, 0x04, 0, 7, macid)                # MACID      word1[0:7]
    _set_le32_bits(d, 0x04, 8, 5, qsel)                 # QSEL       word1[8:13]
    _set_le32_bits(d, 0x04, 16, 5, rate_id)             # RATE_ID    word1[16:21]
    _set_le32_bits(d, 0x0C, 8, 1, 1)                    # USE_RATE   word3[8]
    _set_le32_bits(d, 0x10, 0, 7, hw_rate)              # DATARATE   word4[0:7] (fixed)
    _set_le32_bits(d, 0x20, 15, 1, 1)                   # EN_HWSEQ   word8[15]
    chksum = 0
    for i in range(16):                                 # XOR-16 over the first 32 bytes
        chksum ^= struct.unpack_from("<H", d, 2 * i)[0]
    _set_le32_bits(d, 0x1C, 0, 16, chksum)              # TXDESC_CHECKSUM word7[0:16]
    return bytes(d) + frame
