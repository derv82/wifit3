"""Tests for the TX path: tx_pkt_desc layout + checksum + ep mapping."""
from __future__ import annotations

import struct

from wifit3.chips.rtl8821au.tx import (
    TX_DESC_QSEL_MGMT,
    TX_PKT_DESC_SZ,
    build_deauth_frame,
    build_tx_desc_mgmt,
    pick_bulk_out_ep,
)


def _word(desc: bytes, idx: int) -> int:
    return struct.unpack_from("<I", desc, idx * 4)[0]


def test_desc_is_40_bytes():
    desc = build_tx_desc_mgmt(b"\x00" * 26)
    assert len(desc) == TX_PKT_DESC_SZ


def test_desc_w0_pkt_size_and_flags():
    """W0: TXPKTSIZE[15:0], OFFSET[23:16]=40, BMC[24]=0, LS[26]=1, DISQSELSEQ[31]=1."""
    desc = build_tx_desc_mgmt(b"\x00" * 26)
    w0 = _word(desc, 0)
    assert w0 & 0xFFFF == 26
    assert (w0 >> 16) & 0xFF == 40
    assert (w0 >> 24) & 1 == 0     # unicast (addr1[0]=0)
    assert (w0 >> 26) & 1 == 1     # LS
    assert (w0 >> 31) & 1 == 1     # DISQSELSEQ


def test_desc_w0_bmc_set_for_multicast():
    """addr1[0] bit 0 = the I/G bit → BMC=1."""
    # Multicast addr1 = 01:00:5e:...
    frame = b"\xC0\x00\x00\x00" + b"\x01\x00\x5e\x00\x00\x01" + b"\x00" * 16
    desc = build_tx_desc_mgmt(frame)
    w0 = _word(desc, 0)
    assert (w0 >> 24) & 1 == 1     # BMC


def test_desc_w1_mgmt_qsel_and_rate_id_2g():
    desc = build_tx_desc_mgmt(b"\x00" * 26, band_is_2g=True)
    w1 = _word(desc, 1)
    assert (w1 >> 8) & 0x1F == TX_DESC_QSEL_MGMT
    assert (w1 >> 16) & 0x1F == 8  # RTW_RATEID_B_20M


def test_desc_w3_use_rate_and_disdatafb():
    desc = build_tx_desc_mgmt(b"\x00" * 26)
    w3 = _word(desc, 3)
    assert (w3 >> 8) & 1 == 1      # USE_RATE
    assert (w3 >> 10) & 1 == 1     # DISDATAFB


def test_desc_w4_rate_is_1m_for_2g():
    desc = build_tx_desc_mgmt(b"\x00" * 26, band_is_2g=True)
    w4 = _word(desc, 4)
    assert w4 & 0x7F == 0          # DESC_RATE1M = 0
    assert (w4 >> 8) & 0x1F == 0x1F  # FB_LIMIT (old_datarate_fb_limit path)


def test_desc_w8_en_hwseq():
    desc = build_tx_desc_mgmt(b"\x00" * 26)
    w8 = _word(desc, 8)
    assert (w8 >> 15) & 1 == 1


def test_desc_checksum_xors_first_32_bytes():
    """Checksum: XOR the first 16 u16 (32 bytes) into W7[15:0].

    Independently compute the expected checksum from the desc with W7
    zeroed and confirm it lands in the right field.
    """
    desc = build_tx_desc_mgmt(b"\x00" * 26)
    # Recompute: zero W7's checksum field and XOR all 16 u16s.
    desc_z = bytearray(desc)
    struct.pack_into("<H", desc_z, 7 * 4, 0)
    chk = 0
    for i in range(16):
        chk ^= struct.unpack_from("<H", desc_z, i * 2)[0]
    stored = struct.unpack_from("<H", desc, 7 * 4)[0]
    assert stored == chk


def test_pick_bulk_out_ep_mgmt_to_index_1():
    """MGMT → NORMAL → ep_index 1. AWUS036ACS exposes [0x05, 0x06, ...]."""
    eps = [0x05, 0x06, 0x08, 0x09]
    assert pick_bulk_out_ep(eps, queue=TX_DESC_QSEL_MGMT) == 0x06


def test_pick_bulk_out_ep_falls_back_when_idx_oob():
    """If only 1 bulk-OUT is exposed, MGMT idx=1 is OOB → fallback to [0]."""
    eps = [0x05]
    assert pick_bulk_out_ep(eps, queue=TX_DESC_QSEL_MGMT) == 0x05


def test_build_deauth_frame_layout():
    ap = b"\x30\x85\xa9\x39\xd2\x18"
    cl = b"\x04\x2e\xc1\x51\x43\xb8"
    frame = build_deauth_frame(ap, cl, reason=7)
    assert len(frame) == 26
    assert frame[0:2] == b"\xC0\x00"            # FC
    assert frame[4:10] == cl                     # addr1 = client (dest)
    assert frame[10:16] == ap                    # addr2 = AP (source, spoofed)
    assert frame[16:22] == ap                    # addr3 = BSSID
    assert frame[24:26] == b"\x07\x00"           # reason 7 (LE)
