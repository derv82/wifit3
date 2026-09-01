"""Hardware-free regression for the RTL8822CU MGMT TX descriptor.

The 8822c tx_pkt_desc is the same 48-byte layout as 8822b.  These tests pin
the shared field bit positions (tx_common.fill_txdesc_checksum, the
dma_mapping->endpoint lookup) and the 3-bulk-OUT endpoint mapping measured
on the real 2357:0137 adapter ([0x05, 0x06, 0x08]).
"""
import struct
from unittest.mock import MagicMock

import pytest

from wifit3.chips.rtl8822cu.tx import (
    TX_DESC_QSEL_MGMT,
    build_tx_desc_inject,
    build_tx_desc_mgmt,
    pick_bulk_out_ep,
    write_bulk,
)
from wifit3.chips.rtw88_base.registers import (
    DESC_RATE1M,
    DESC_RATE6M,
    TX_DESC_QSEL_BEACON,
    TX_DESC_QSEL_H2C,
    TX_DESC_QSEL_HIGH,
)


def _field(desc: bytes, word: int, bit_start: int, bit_len: int) -> int:
    value = struct.unpack_from("<I", desc, word * 4)[0]
    return (value >> bit_start) & ((1 << bit_len) - 1)


def _deauth(addr1: bytes = b"\x02\x11\x11\x11\x11\x11") -> bytes:
    """A 26-byte deauth MPDU (FC + duration + 3 addrs + seq + reason)."""
    return b"\xc0\x00\x00\x00" + addr1 + b"\x22" * 6 + b"\x33" * 6 + b"\x00\x00" + b"\x07\x00"


def test_tx_desc_is_48_bytes_and_checksummed():
    desc = build_tx_desc_mgmt(_deauth())
    assert len(desc) == 48
    stored = struct.unpack_from("<H", desc, 28)[0]
    assert stored != 0
    # Re-deriving the checksum over the descriptor (its field zeroed) reproduces it.
    body = bytearray(desc)
    body[28:30] = b"\x00\x00"
    chksum = 0
    for i in range(16):
        chksum ^= struct.unpack_from("<H", body, i * 2)[0]
    assert chksum & 0xFFFF == stored


def test_tx_desc_mgmt_default_2g_fields():
    desc = build_tx_desc_mgmt(_deauth(), band_is_2g=True)
    assert _field(desc, 0, 0, 16) == 26              # TXPKTSIZE
    assert _field(desc, 0, 16, 8) == 48             # OFFSET = TX_PKT_DESC_SZ
    assert _field(desc, 0, 24, 1) == 0              # BMC off (unicast addr1)
    assert _field(desc, 0, 26, 1) == 1              # LS
    assert _field(desc, 0, 31, 1) == 1              # DISQSELSEQ
    assert _field(desc, 1, 8, 5) == TX_DESC_QSEL_MGMT
    assert _field(desc, 1, 16, 5) == 8              # RTW_RATEID_B_20M
    assert _field(desc, 3, 8, 1) == 1               # USE_RATE
    assert _field(desc, 3, 10, 1) == 1              # DISDATAFB
    assert _field(desc, 4, 0, 7) == DESC_RATE1M     # 1 Mbps CCK on 2.4 GHz
    assert _field(desc, 8, 15, 1) == 1              # EN_HWSEQ (HW stamps seq)


def test_tx_desc_5g_uses_ofdm_rate():
    desc = build_tx_desc_mgmt(_deauth(), band_is_2g=False)
    assert _field(desc, 4, 0, 7) == DESC_RATE6M
    assert _field(desc, 1, 16, 5) == 7              # RTW_RATEID_G


def test_tx_desc_broadcast_sets_bmc():
    desc = build_tx_desc_mgmt(_deauth(addr1=b"\xff" * 6), band_is_2g=True)
    assert _field(desc, 0, 24, 1) == 1              # BMC set for broadcast


def test_tx_desc_retry_limit_fields():
    desc = build_tx_desc_mgmt(_deauth(), retry_limit=12)
    assert _field(desc, 4, 17, 1) == 1              # RTY_LMT_EN
    assert _field(desc, 4, 18, 6) == 12             # RTS_DATA_RTY_LMT
    plain = build_tx_desc_mgmt(_deauth())
    assert _field(plain, 4, 17, 1) == 0             # HW global retry by default
    assert _field(plain, 4, 18, 6) == 0


def test_short_mpdu_rejected():
    with pytest.raises(ValueError):
        build_tx_desc_mgmt(b"\xc0\x00" * 4)


# The first injected bulk-OUT frame in capture-1 (op #25502): a 42-byte broadcast probe request.
_CAPTURE_INJECT_FRAME = bytes.fromhex(
    "40000000ffffffffffff00b13eaed09bffffffffffff00000000"
    "010402040b1632080c1218243048606c"
)


def test_inject_desc_deterministic_fields_match_capture():
    desc = build_tx_desc_inject(_CAPTURE_INJECT_FRAME, band_is_2g=True)
    assert len(desc) == 48
    assert _field(desc, 0, 0, 16) == 42             # TXPKTSIZE = frame length
    assert _field(desc, 0, 16, 8) == 48             # OFFSET
    assert _field(desc, 0, 24, 1) == 1              # BMC (broadcast addr1)
    assert _field(desc, 0, 26, 1) == 1              # LS
    assert _field(desc, 0, 31, 1) == 0              # DISQSELSEQ/OWN clear for inject
    assert _field(desc, 1, 0, 7) == 1               # MACID (RTW_DEFAULT_MGMT_MACID)
    assert _field(desc, 1, 8, 5) == TX_DESC_QSEL_MGMT   # QSEL 0x12
    assert _field(desc, 1, 16, 5) == 9              # RATE_ID (RATEID_IDX_VHT_2SS)
    assert _field(desc, 3, 8, 1) == 1               # USE_RATE
    assert _field(desc, 3, 9, 1) == 1               # DISRTSFB
    assert _field(desc, 3, 10, 1) == 1              # DISDATAFB
    assert _field(desc, 4, 0, 7) == DESC_RATE1M     # 1 Mbps CCK basic rate on 2.4 GHz
    assert _field(desc, 4, 17, 1) == 1              # RTY_LMT_EN
    assert _field(desc, 8, 15, 1) == 0              # EN_HWSEQ clear (chip keeps frame seq)


def test_inject_desc_checksum_spans_full_48_bytes():
    desc = build_tx_desc_inject(_CAPTURE_INJECT_FRAME)
    stored = struct.unpack_from("<H", desc, 28)[0]
    body = bytearray(desc)
    body[28:30] = b"\x00\x00"
    chksum = 0
    for i in range(24):                             # 24 u16 words = the whole 48-byte descriptor
        chksum ^= struct.unpack_from("<H", body, i * 2)[0]
    assert chksum & 0xFFFF == stored


def test_inject_desc_takes_seq_from_frame_not_hwseq():
    frame = bytearray(_CAPTURE_INJECT_FRAME)
    struct.pack_into("<H", frame, 22, 0x0A70)       # seqnum 0xA7, frag 0
    desc = build_tx_desc_inject(bytes(frame))
    assert _field(desc, 9, 12, 12) == 0xA7          # SW_SEQ copied from the frame's seq_ctl
    assert _field(desc, 8, 15, 1) == 0              # EN_HWSEQ stays clear


def test_inject_desc_uses_raw_monitor_1m_rate():
    # The raw monitor inject path mirrors rtw_monitor_xmit_entry, whose fixed_rate default
    # is MGN_1M -> DESC_RATE1M on both bands (no radiotap rate is parsed). This differs from
    # build_tx_desc_mgmt, which is band aware (6M OFDM on 5 GHz). Whether the inject path
    # should also be band aware for 5 GHz is an open decision: NEEDS-DERV INJ-1.
    for band_2g in (True, False):
        desc = build_tx_desc_inject(_CAPTURE_INJECT_FRAME, band_is_2g=band_2g)
        assert _field(desc, 4, 0, 7) == DESC_RATE1M


def test_inject_desc_differs_from_mgmt_on_seq_and_own():
    inject = build_tx_desc_inject(_CAPTURE_INJECT_FRAME)
    mgmt = build_tx_desc_mgmt(_CAPTURE_INJECT_FRAME)
    assert _field(inject, 0, 31, 1) == 0 and _field(mgmt, 0, 31, 1) == 1   # DISQSELSEQ
    assert _field(inject, 8, 15, 1) == 0 and _field(mgmt, 8, 15, 1) == 1   # EN_HWSEQ


def test_pick_bulk_out_ep_maps_high_queues_to_first_pipe():
    out_eps = [0x05, 0x06, 0x08]
    for queue in (TX_DESC_QSEL_BEACON, TX_DESC_QSEL_HIGH, TX_DESC_QSEL_MGMT, TX_DESC_QSEL_H2C):
        assert pick_bulk_out_ep(out_eps, queue=queue) == 0x05


def test_pick_bulk_out_ep_maps_normal_queue_to_second_pipe():
    out_eps = [0x05, 0x06, 0x08]
    assert pick_bulk_out_ep(out_eps, queue=0) == 0x06      # BE/BK -> NORMAL


def test_pick_bulk_out_ep_falls_back_to_first_pipe():
    # A single-pipe layout can only satisfy the HIGH mapping; NORMAL falls back.
    assert pick_bulk_out_ep([0x05], queue=TX_DESC_QSEL_MGMT) == 0x05
    assert pick_bulk_out_ep([0x05], queue=0) == 0x05


def test_write_bulk_returns_sent_count():
    dev = MagicMock()
    dev.write.return_value = 260
    assert write_bulk(dev, 0x05, b"x" * 260) == 260
    dev.write.assert_called_once_with(0x05, b"x" * 260, 200)
