"""rtl8188ftv M7 TX acceptance: txdesc40 MGMT build, csum, deauth frame.

Covers the values computed from kernel C (core.c:5128-5141, 5340-5406,
5530-5565; 8188f.c:1735) independent of the capture: descriptor layout,
MGMT queue routing, SW sequence mirroring into txdw9, and the XOR-16
checkum.  Live wire-order replay of the broadcast deauth dumps is not
gated in verify_pcap.py (no injection in the scan pcap).
"""
import struct

from wifit3.chips.rtl8188ftv.constants import (
    FC0_SUBTYPE_DEAUTH,
    FC0_TYPE_MGMT,
    REASON_CODE_CLASS3_FRAME,
    TX_DESC_SZ_8188F,
    TXDESC40_AGG_BREAK,
    TXDESC40_RETRY_LIMIT_ENABLE,
    TXDESC40_RETRY_LIMIT_MGNT,
    TXDESC40_RETRY_LIMIT_SHIFT,
    TXDESC40_SEQ_SHIFT,
    TXDESC40_USE_DRIVER_RATE,
    TXDESC_BROADMULTICAST,
    TXDESC_FIRST_SEGMENT,
    TXDESC_LAST_SEGMENT,
    TXDESC_OWN,
    TXDESC_QUEUE_MGNT,
    TXDESC_QUEUE_SHIFT,
)
from wifit3.chips.rtl8188ftv.tx import build_deauth, build_tx_desc_mgmt, calc_tx_desc_csum


def test_build_tx_desc_mgmt_layout():
    desc = build_tx_desc_mgmt(pkt_len=30, is_broadcast=True)
    assert isinstance(desc, bytearray)
    assert len(desc) == TX_DESC_SZ_8188F

    pkt_size = struct.unpack_from("<H", desc, 0)[0]
    assert pkt_size == 30
    assert desc[2] == TX_DESC_SZ_8188F          # pkt_offset = descriptor size

    txdw0 = desc[3]
    assert txdw0 & TXDESC_OWN
    assert txdw0 & TXDESC_FIRST_SEGMENT
    assert txdw0 & TXDESC_LAST_SEGMENT
    assert txdw0 & TXDESC_BROADMULTICAST


def test_build_tx_desc_mgmt_multicast_clears_no_broadcast_flag():
    desc = build_tx_desc_mgmt(pkt_len=30, is_broadcast=False)
    assert not (desc[3] & TXDESC_BROADMULTICAST)
    assert not (desc[3] & TXDESC_BROADMULTICAST)


def test_build_tx_desc_mgmt_queue_and_flags():
    desc = build_tx_desc_mgmt(pkt_len=26, is_broadcast=False)
    txdw1 = struct.unpack_from("<I", desc, 4)[0]
    assert (txdw1 >> TXDESC_QUEUE_SHIFT) & 0xFF == TXDESC_QUEUE_MGNT
    txdw2 = struct.unpack_from("<I", desc, 8)[0]
    assert txdw2 & TXDESC40_AGG_BREAK
    txdw3 = struct.unpack_from("<I", desc, 12)[0]
    assert txdw3 & TXDESC40_USE_DRIVER_RATE


def test_build_tx_desc_mgmt_retry_limit():
    desc = build_tx_desc_mgmt(pkt_len=26, is_broadcast=False,
                              retry_limit=TXDESC40_RETRY_LIMIT_MGNT)
    txdw4 = struct.unpack_from("<I", desc, 16)[0]
    limit = (txdw4 >> TXDESC40_RETRY_LIMIT_SHIFT) & 0x3F
    assert limit == TXDESC40_RETRY_LIMIT_MGNT
    assert txdw4 & TXDESC40_RETRY_LIMIT_ENABLE


def test_build_tx_desc_mgmt_seq_mirrors_into_txdw9():
    seq = 0xABC
    desc = build_tx_desc_mgmt(pkt_len=26, is_broadcast=False, seq=seq)
    txdw9 = struct.unpack_from("<I", desc, 36)[0]
    assert (txdw9 >> TXDESC40_SEQ_SHIFT) & 0xFFF == seq


def test_calc_tx_desc_csum_xor16():
    desc = build_tx_desc_mgmt(pkt_len=26, is_broadcast=False)
    calc_tx_desc_csum(desc)
    # XOR-16 over the first 32 bytes with csum cleared must be zero
    # (core.c:5140: sizeof(struct rtl8xxxu_txdesc32) / sizeof(u16)).
    replayed = 0
    for i in range(0, 32, 2):
        replayed ^= struct.unpack_from("<H", desc, i)[0]
    assert replayed == 0


def test_calc_tx_desc_csum_excludes_txdw9():
    # A nonzero SW-stamped seq lives in txdw9 (bytes 36-39), which the kernel's
    # csum window (first 32 bytes) does NOT cover; the chip would drop the frame
    # if txdw9 leaked into the checksum. Verify the csum stays unchanged when
    # only txdw9 varies.
    lo = build_tx_desc_mgmt(pkt_len=26, is_broadcast=False, seq=0)
    hi = build_tx_desc_mgmt(pkt_len=26, is_broadcast=False, seq=0xFFF)
    calc_tx_desc_csum(lo)
    calc_tx_desc_csum(hi)
    assert bytes(lo[:32]) == bytes(hi[:32])          # csum covers only the first 32 bytes
    assert lo[32:36] == b"\x00\x00\x00\x00"          # txdw8 stays zero in both
    assert hi[36:39] != lo[36:39]                    # txdw9 differs (carries the seq)


def test_build_deauth_wire_layout():
    bssid = bytes.fromhex("001122334455")
    client = bytes.fromhex("aabbccddeeff")
    frame = build_deauth(bssid, client, reason=REASON_CODE_CLASS3_FRAME)
    assert frame[0] & 0x0C == FC0_TYPE_MGMT
    assert (frame[0] & 0xF0) >> 4 == FC0_SUBTYPE_DEAUTH >> 4
    assert frame[16:22] == bssid   # addr3 (BSSID)
    assert frame[10:16] == bssid   # addr2 (source = the AP we spoof)
    assert frame[4:10] == client   # addr1 (destination)
    seq_ctrl = struct.unpack_from("<H", frame, 22)[0]
    assert seq_ctrl == 0           # stamped per inject, zero in the builder
    reason = struct.unpack_from("<H", frame, 24)[0]
    assert reason == REASON_CODE_CLASS3_FRAME