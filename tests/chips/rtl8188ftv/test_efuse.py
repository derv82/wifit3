"""rtl8188ftv EFUSE parse + wire-format replay.

The parse tests decode a synthetic raw map built from the real device's
captured values (see scripts/chips/rtl8188ftv/verify_pcap.py, EFUSE gate).
"""
import pytest

from wifit3.chips.rtl8188ftv.constants import (
    TX_POWER_INDEX_DEFAULT_CCK,
    TX_POWER_INDEX_DEFAULT_HT40,
)
from wifit3.chips.rtl8188ftv.efuse import (
    _MAX_CHANNEL_GROUPS,
    parse_efuse_8188fu,
)

# Layout derived from struct rtl8188fu_efuse (rtl8xxxu.h:1166-1208).
_RTL_ID = 0x8129
_MAC = bytes.fromhex("44efbf1f9dfb")
_XTAL = 0x1D
_CCK = (0x1D, 0x1E, 0x20, 0x20, 0x21, 0x21)
_HT40 = (0x20, 0x20, 0x23, 0x23, 0x25, 0x00)  # 5 efuse bytes + 0-fill slot


def _raw_map(**overrides) -> bytes:
    b = bytearray(b"\xff" * 512)
    b[0] = _RTL_ID & 0xFF
    b[1] = (_RTL_ID >> 8) & 0xFF
    b[0x10:0x16] = _CCK
    b[0x16:0x1B] = _HT40[:5]
    b[0x1B] = 0x13  # rtl8723au_idx: a=3 (ofdm), b=1 (ht20), both +ve
    b[0xD7:0xDD] = _MAC
    b[0xB9] = _XTAL
    for off, val in overrides.items():
        b[off] = val
    return bytes(b)


def test_parse_efuse_extracts_mac_power_and_crystal():
    p = parse_efuse_8188fu(_raw_map())
    assert p.mac_address == _MAC
    assert p.cck_tx_power_index_A == _CCK
    assert p.ht40_1s_tx_power_index_A == _HT40
    assert p.ofdm_tx_power_diff_a == 3
    assert p.ht20_tx_power_diff_a == 1
    assert p.default_crystal_cap == _XTAL


def test_parse_efuse_rejects_foreign_rtl_id():
    raw = _raw_map()
    raw = bytes(raw[:1]) + b"\x99" + raw[2:]
    with pytest.raises(ValueError):
        parse_efuse_8188fu(raw)


def test_parse_efuse_clamps_out_of_range_power_indexes():
    raw = _raw_map()
    raw = bytes(raw[:0x10]) + bytes([0x7F, 0xFF, 0x40, 0x3F, 0x00, 0x22]) + raw[0x16:]
    p = parse_efuse_8188fu(raw)
    assert p.cck_tx_power_index_A == (
        TX_POWER_INDEX_DEFAULT_CCK,  # 0x7F > 0x3F
        TX_POWER_INDEX_DEFAULT_CCK,  # 0xFF -> default too
        TX_POWER_INDEX_DEFAULT_CCK,  # 0x40 > 0x3F
        0x3F,                        # 0x3F is in range, kept as-is
        0x00,                        # 0x00 in range
        0x22,
    )


def test_parse_efuse_blank_map_falls_back_to_defaults():
    p = parse_efuse_8188fu(_raw_map())
    # Force every power byte to 0xFF except the rtl_id guard: the kernel's
    # "> TX_POWER_INDEX_MAX" rule then substitutes the per-band defaults.
    raw = bytearray(p.raw)
    for off in range(0x10, 0x1B):
        raw[off] = 0xFF
    p2 = parse_efuse_8188fu(bytes(raw))
    assert p2.cck_tx_power_index_A == (TX_POWER_INDEX_DEFAULT_CCK,) * _MAX_CHANNEL_GROUPS
    # Kernel memcpy of 5 efuse ht40 bytes into a 6-slot array leaves slot 5
    # at its zeroed init value (rtl8xxxu.h:1813 ARRAY_SIZE = 6).
    assert p2.ht40_1s_tx_power_index_A == (
        TX_POWER_INDEX_DEFAULT_HT40,) * 5 + (0,)