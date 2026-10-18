"""PHY/MAC/AGC/RF init tables as extracted from 8192e.c (v6.19 source base).

Lengths are the extractor's exact output; a drift here means either the kernel
source or ``scripts/chips/rtl8192eu/extract_phy_tables.py`` changed.
"""
from __future__ import annotations

import pytest

from wifit3.chips.rtl8192eu import phy_tables as tables


EXPECTED_LENGTHS = {
    "MAC_INIT_TABLE_8192E": 101,
    "PHY_INIT_TABLE_8192E": 208,
    "AGC_STD_TABLE_8192E": 130,
    "AGC_HIGHP_A_TABLE_8192E": 130,
    "RADIO_A_INIT_TABLE_8192E": 97,
    "RADIO_B_INIT_TABLE_8192E": 77,
}


@pytest.mark.parametrize(("name", "length"), EXPECTED_LENGTHS.items())
def test_table_length(name, length):
    assert len(getattr(tables, name)) == length


@pytest.mark.parametrize("name", EXPECTED_LENGTHS)
def test_table_entries_are_register_value_pairs(name):
    for reg, val in getattr(tables, name):
        assert 0 <= reg <= 0xFFFF
        assert 0 <= val <= 0xFFFFFFFF


def test_mac_init_table_boundaries():
    assert tables.MAC_INIT_TABLE_8192E[0] == (0x011, 0x000000EB)
    assert tables.MAC_INIT_TABLE_8192E[-1] == (0x70B, 0x00000087)
    # REG_MAR all-ones: accept every multicast/broadcast address (core.c:4148-4154).
    mar = [v for reg, v in tables.MAC_INIT_TABLE_8192E if reg == 0x620]
    assert mar == [0xFF]


def test_phy_init_table_boundaries():
    assert tables.PHY_INIT_TABLE_8192E[0] == (0x800, 0x80040000)
    assert tables.PHY_INIT_TABLE_8192E[-1] == (0xF00, 0x00000300)


def test_agc_tables_are_the_same_shape_with_a_high_pa_delta():
    std = tables.AGC_STD_TABLE_8192E
    highp = tables.AGC_HIGHP_A_TABLE_8192E
    assert [reg for reg, _ in std] == [reg for reg, _ in highp]
    # 128 AGC-index writes to 0xC78, then the two 0xC50 CCA-threshold pokes.
    assert [reg for reg, _ in std].count(0xC78) == 128
    assert std[-2:] == ((0xC50, 0x00040022), (0xC50, 0x00040020))
    assert highp[-2:] == ((0xC50, 0x00040222), (0xC50, 0x00040220))


def test_radio_tables_share_the_same_head_and_tail():
    a = tables.RADIO_A_INIT_TABLE_8192E
    b = tables.RADIO_B_INIT_TABLE_8192E
    assert a[0] == b[0] == (0x7F, 0x00000082)
    assert a[-1] == b[-1] == (0x00, 0x00033E70)


def test_radio_tables_differ_on_the_path_specific_registers():
    a_86 = [v for reg, v in tables.RADIO_A_INIT_TABLE_8192E if reg == 0x86]
    b_86 = [v for reg, v in tables.RADIO_B_INIT_TABLE_8192E if reg == 0x86]
    assert a_86 == [0x0000014E]
    assert b_86 == [0x000000CE]

    a_00 = [v for reg, v in tables.RADIO_A_INIT_TABLE_8192E if reg == 0x00]
    b_00 = [v for reg, v in tables.RADIO_B_INIT_TABLE_8192E if reg == 0x00]
    assert 0x00010159 in b_00 and 0x00010159 not in a_00


def test_radio_delay_opcodes_are_embedded():
    """0xFE is msleep(50) in init_rf_regs, not a SIPI address (core.c:2400-2418)."""
    a = tables.RADIO_A_INIT_TABLE_8192E
    b = tables.RADIO_B_INIT_TABLE_8192E
    assert sum(1 for reg, _ in a if reg == 0xFE) == 5
    assert sum(1 for reg, _ in b if reg == 0xFE) == 4
