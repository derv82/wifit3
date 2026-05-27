"""Tests for the RTL8814AU TX-descriptor builder + deauth frame (pure logic)."""
from __future__ import annotations

import struct

from wifit3.chips.rtw88_base.registers import (
    DESC_RATE1M,
    DESC_RATE6M,
    TX_DESC_QSEL_MGMT,
)
from wifit3.chips.rtw88_8814au.tx import build_deauth_frame, build_tx_desc_mgmt

_AP = bytes.fromhex("aa:bb:cc:dd:ee:01")
_CLIENT = bytes.fromhex("042ec15143b8")


class TestTxDesc:
    def test_length_is_40(self):
        desc = build_tx_desc_mgmt(b"\x00" * 26, band_is_2g=True)
        assert len(desc) == 40

    def test_pkt_size_and_offset(self):
        mpdu = b"\xc0\x00" + b"\x11" * 24      # 26-byte deauth-ish
        w0 = struct.unpack_from("<I", build_tx_desc_mgmt(mpdu), 0)[0]
        assert (w0 & 0xFFFF) == len(mpdu)       # TXPKTSIZE
        assert ((w0 >> 16) & 0xFF) == 40        # OFFSET = desc size

    def test_2g_uses_cck_1m(self):
        w4 = struct.unpack_from("<I", build_tx_desc_mgmt(b"\x00" * 26, band_is_2g=True), 16)[0]
        assert (w4 & 0x7F) == DESC_RATE1M

    def test_5g_uses_ofdm_6m(self):
        w4 = struct.unpack_from("<I", build_tx_desc_mgmt(b"\x00" * 26, band_is_2g=False), 16)[0]
        assert (w4 & 0x7F) == DESC_RATE6M

    def test_qsel_is_mgmt(self):
        w1 = struct.unpack_from("<I", build_tx_desc_mgmt(b"\x00" * 26), 4)[0]
        assert ((w1 >> 8) & 0x1F) == TX_DESC_QSEL_MGMT

    def test_broadcast_sets_bmc(self):
        bcast = b"\xc0\x00\x00\x00" + b"\xff" * 6 + b"\x00" * 16
        w0 = struct.unpack_from("<I", build_tx_desc_mgmt(bcast), 0)[0]
        assert (w0 >> 24) & 1 == 1               # BMC bit for broadcast addr1


class TestDeauthFrame:
    def test_addressing_and_type(self):
        f = build_deauth_frame(_AP, _CLIENT, reason=7)
        assert f[0:2] == b"\xc0\x00"             # mgmt / deauth subtype
        assert f[4:10] == _CLIENT                # addr1 = DA (client)
        assert f[10:16] == _AP                   # addr2 = SA (ap)
        assert f[16:22] == _AP                   # addr3 = BSSID (ap)
        assert struct.unpack_from("<H", f, 24)[0] == 7   # reason code
