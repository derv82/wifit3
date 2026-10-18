"""EFUSE: map walk preconditions + the 8192eu per-chip parse layout."""
from __future__ import annotations

import pytest

from wifit3.chips.rtl8192eu.constants import (
    EFUSE_ACCESS_ENABLE,
    EFUSE_MAP_LEN,
    REG_EFUSE_ACCESS,
)
from wifit3.chips.rtl8192eu.efuse import (
    EfuseDefaults,
    parse_efuse_8192eu,
    read_efuse_map,
)

MAC = bytes.fromhex("c006c3c9ec0b")


def make_efuse_map() -> bytes:
    raw = bytearray(b"\xFF" * EFUSE_MAP_LEN)
    raw[0:2] = (0x8129).to_bytes(2, "little")     # rtl_id 8192eu

    for block in (0x10, 0x3A):                     # tx_power A, tx_power B
        raw[block : block + 6] = bytes([0x10, 0x11, 0x12, 0x13, 0x14, 0x15])
        raw[block + 6 : block + 11] = bytes([0x20, 0x21, 0x22, 0x23, 0x24])
        raw[block + 0x0B] = 0xF3                   # ht20/ofdm 1s diff: b = -1
        for i in range(3):
            raw[block + 0x0C + 2 * i] = 0x21       # ht20=1, ht40=2
            raw[block + 0x0D + 2 * i] = 0x43       # ofdm=3, cck=4

    raw[0xB9] = 0x2A                               # xtal_k (& 0x3F -> 0x2A)
    raw[0xD7 : 0xD7 + 6] = MAC
    return bytes(raw)


def test_parse_extracts_mac_crystal_cap_and_power_bases():
    parsed = parse_efuse_8192eu(make_efuse_map())

    assert parsed.mac_address == MAC
    assert parsed.default_crystal_cap == 0x2A
    assert parsed.cck_tx_power_index_A == (0x10, 0x11, 0x12, 0x13, 0x14, 0x15)
    assert parsed.cck_tx_power_index_B == (0x10, 0x11, 0x12, 0x13, 0x14, 0x15)
    # ht40_base is 5 bytes in the struct; the 6th priv slot stays zero.
    assert parsed.ht40_1s_tx_power_index_A == (0x20, 0x21, 0x22, 0x23, 0x24, 0)
    assert parsed.ht40_1s_tx_power_index_B == (0x20, 0x21, 0x22, 0x23, 0x24, 0)


def test_parse_power_diffs_use_the_per_path_nibbles():
    parsed = parse_efuse_8192eu(make_efuse_map())

    # index 0 is the single burned HT20/OFDM offset; .b is the used nibble.
    assert parsed.ht20_tx_power_diff[0] == (-1, -1)
    assert parsed.ofdm_tx_power_diff[0] == (0, 0)
    assert parsed.ht40_tx_power_diff[0] == (0, 0)

    for i in range(1, 4):
        assert parsed.ofdm_tx_power_diff[i] == (3, 3)
        assert parsed.ht20_tx_power_diff[i] == (1, 1)
        assert parsed.ht40_tx_power_diff[i] == (2, 2)


def test_parse_keeps_the_raw_map():
    raw = make_efuse_map()
    assert parse_efuse_8192eu(raw).raw == raw


def test_parse_rejects_a_short_map():
    with pytest.raises(ValueError, match="too short"):
        parse_efuse_8192eu(b"\x00" * (EFUSE_MAP_LEN - 1))


def test_parse_rejects_a_non_8192eu_fuse():
    raw = bytearray(make_efuse_map())
    raw[0:2] = (0x8179).to_bytes(2, "little")
    with pytest.raises(ValueError, match="not 8192eu"):
        parse_efuse_8192eu(bytes(raw))


def test_defaults_are_the_0x22_fallback():
    defaults = EfuseDefaults()
    assert defaults.cck_tx_power_index_A == (0x22,) * 6
    assert defaults.ht40_1s_tx_power_index_B == (0x22,) * 6
    assert defaults.ht20_tx_power_diff == ((0, 0),) * 4
    assert defaults.default_crystal_cap == 0
    assert defaults.mac_address is None


def test_read_efuse_map_stops_on_the_first_blank_byte():
    """An all-0xFF map walks zero words and leaves the 512-byte buffer untouched."""
    from wifit3.chips.rtl8192eu.constants import REG_EFUSE_CTRL

    from tests.chips.rtl8192eu._mock import MockTransport

    class EfuseReady(MockTransport):
        """HW sets CTRL[31] when the trigger byte lands; the data byte stays 0xFF."""

        def write8(self, addr: int, val: int) -> None:
            super().write8(addr, val)
            if addr == REG_EFUSE_CTRL + 3:
                self.regs[addr] |= 0x80

    t = EfuseReady()
    t.seed(REG_EFUSE_CTRL, 0xFF, 1)          # first EFUSE byte = header 0xFF -> stop

    raw = read_efuse_map(t)

    assert raw == b"\xFF" * EFUSE_MAP_LEN
    # Access window is opened and always closed, even on the early stop.
    assert t.writes_to(REG_EFUSE_ACCESS)[0][2] == EFUSE_ACCESS_ENABLE
    assert t.writes_to(REG_EFUSE_ACCESS)[-1][2] == 0
