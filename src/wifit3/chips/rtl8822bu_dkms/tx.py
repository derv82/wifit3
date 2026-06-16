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


def build_inject_txdesc(frame: bytes, *, qsel: int = TXDESC_QSEL_MGNT, macid: int = 1,
                        hw_rate: int = DESC_RATE1M, rate_id: int = RATEID_IDX_B) -> bytes:
    """48-byte injector descriptor prepended to `frame` = the bulk-OUT payload.

    Reproduces the **`update_txdesc` MGNT branch** [SRC] rtl8822bu_xmit.c — the path the kernel's
    `rtw_mgnt_xmit` (aireplay-ng's route) actually takes, byte-diffed against the captured injector:
    LS + OFFSET=48 + TXPKTSIZE, MACID = `RTW_DEFAULT_MGMT_MACID` (1, the bcast/self station), QSEL=MGNT,
    RATE_ID, USE_RATE + DATARATE (fixed rate), RTY_LMT_EN + RTS_DATA_RTY_LMT=12 (no retry-ctrl), G_ID =
    the RA station's beamforming group (63 broadcast / 0 unicast; wifit3 never beamforms), SW_DEFINE=1 (the DriverFixedRate
    flag for the USE_RATE path), DISQSELSEQ + EN_HWSEQ (HW stamps the sequence number; !qos_en). BMC
    (word0[24]) when addr1 (frame[4]) is group-addressed. Field offsets [SRC] halmac_tx_desc_nic.h;
    XOR-16 checksum over the first 32 bytes [SRC] halmac_common_8822b.c fill_txdesc_check_sum_8822b.
    Rides bulk-OUT EP 0x05 (MGNT qsel -> HIGH pipe -> RtOutPipe[0]). Only the frame's own seqctl varies
    per send (HW-assigned), so the 48-byte descriptor is byte-identical to the captured aireplay TX."""
    d = bytearray(TX_DESC_SIZE_88XX)
    _set_le32_bits(d, 0x00, 0, 16, len(frame))          # TXPKTSIZE  word0[0:16]
    _set_le32_bits(d, 0x00, 16, 8, TX_DESC_SIZE_88XX)   # OFFSET     word0[16:24] (desc bytes)
    bmc = len(frame) >= 5 and (frame[4] & 0x01)         # group-addressed RA (addr1 multicast bit)
    if bmc:
        _set_le32_bits(d, 0x00, 24, 1, 1)               # BMC        word0[24]
    _set_le32_bits(d, 0x00, 26, 1, 1)                   # LS         word0[26] (last segment)
    _set_le32_bits(d, 0x00, 31, 1, 1)                   # DISQSELSEQ word0[31]
    _set_le32_bits(d, 0x04, 0, 7, macid)                # MACID      word1[0:7]  (=1, bcast mgmt)
    _set_le32_bits(d, 0x04, 8, 5, qsel)                 # QSEL       word1[8:13]
    _set_le32_bits(d, 0x04, 16, 5, rate_id)             # RATE_ID    word1[16:21]
    # G_ID word2[24:30] = txbf_g_id [SRC] rtl8822b_ops.c:3288 / rtw_bf_update_attrib: the BF group of
    # the RA station. No BF station (wifit3 never beamforms): broadcast uses the bcast BF group 63,
    # unicast uses 0. Matches the captured aireplay injector (bcast probe-req G_ID=63, unicast deauth 0).
    _set_le32_bits(d, 0x08, 24, 6, 0x3F if bmc else 0)
    _set_le32_bits(d, 0x0C, 8, 1, 1)                    # USE_RATE   word3[8]
    _set_le32_bits(d, 0x10, 0, 7, hw_rate)              # DATARATE   word4[0:7] (fixed)
    _set_le32_bits(d, 0x10, 17, 1, 1)                   # RTY_LMT_EN word4[17]
    _set_le32_bits(d, 0x10, 18, 6, 12)                  # RTS_DATA_RTY_LMT word4[18:24] (=12)
    _set_le32_bits(d, 0x18, 0, 12, 1)                   # SW_DEFINE  word6[0:12] (DriverFixedRate)
    _set_le32_bits(d, 0x20, 15, 1, 1)                   # EN_HWSEQ   word8[15]
    chksum = 0
    for i in range(16):                                 # XOR-16 over the first 32 bytes
        chksum ^= struct.unpack_from("<H", d, 2 * i)[0]
    _set_le32_bits(d, 0x1C, 0, 16, chksum)              # TXDESC_CHECKSUM word7[0:16]
    return bytes(d) + frame
