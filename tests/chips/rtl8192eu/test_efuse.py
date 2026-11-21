"""Offline check on rtl8192eu_parse_efuse decoding a synthetic efuse map (no hardware)."""
from types import SimpleNamespace

import pytest

from wifit3.chips.rtl8192eu.rtl8192e import rtl8192eu_parse_efuse


def _efuse() -> bytearray:
    efuse = bytearray(b"\xff" * 512)
    efuse[0x00:0x02] = (0x8129).to_bytes(2, "little")  # rtl_id
    efuse[0x10:0x16] = bytes([10, 11, 12, 13, 14, 15])  # tx_power_index_A.cck_base
    efuse[0x16:0x1b] = bytes([20, 21, 22, 23, 24])      # tx_power_index_A.ht40_base
    efuse[0x1b] = 0xf0                                   # ht20_ofdm_1s_diff.b = -1
    efuse[0x45] = 0x20                                   # tx_power_index_B ht20_ofdm_1s_diff.b = 2
    efuse[0x1c] = 0x3e                                   # pwr_diff[0] A: ht20=-2(0xe), ht40=3
    efuse[0x1d] = 0x15                                   # pwr_diff[0] A: ofdm=5, cck=1
    efuse[0xb9] = 0xc0 | 0x2a                            # xtal_k, low 6 bits = 0x2a
    efuse[0xd7:0xdd] = bytes.fromhex("001122334455")    # mac_addr
    return efuse


def test_parse_efuse_extracts_mac_power_and_xtal():
    priv = SimpleNamespace(efuse_wifi=_efuse())
    rtl8192eu_parse_efuse(priv)

    assert priv.mac_addr == bytes.fromhex("001122334455")
    assert priv.cck_tx_power_index_A == [10, 11, 12, 13, 14, 15]
    assert priv.ht40_1s_tx_power_index_A == [20, 21, 22, 23, 24]
    assert priv.default_crystal_cap == 0x2a
    # ht20_ofdm_1s_diff.b, signed 4-bit
    assert priv.ht20_tx_power_diff[0] == (-1, 2)
    # pwr_diff[0] -> index 1, signed nibbles
    assert priv.ofdm_tx_power_diff[1][0] == 5
    assert priv.ht20_tx_power_diff[1][0] == -2
    assert priv.ht40_tx_power_diff[1][0] == 3


def test_parse_efuse_rejects_bad_id():
    efuse = _efuse()
    efuse[0x00:0x02] = (0x1234).to_bytes(2, "little")
    with pytest.raises(ValueError):
        rtl8192eu_parse_efuse(SimpleNamespace(efuse_wifi=efuse))
