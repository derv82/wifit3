"""Tests for the M4c BB/RF/band-switch port.

Uses MockTransport from test_mac_init. Verifies:
- table loaders dispatch the expected cfg counts for rfe=0 defaults
- _cfg_rf encodes the SIPI write correctly: ((addr<<20)|(data&0xFFFFF)) & 0x0FFFFFFF
- switch_band_2g_20mhz writes REG_RRSR low-20-bits = BASIC_RATES_2G
"""
from __future__ import annotations

from wifit3.chips.rtl8821au.assets import agc_tbl, bb_tbl, mac_tbl, rf_a_tbl
from wifit3.chips.rtl8821au.constants import (
    BASIC_RATES_2G,
    BIT_RF_EN,
    BIT_RF_RSTB,
    BIT_RF_SDM_RSTB,
    REG_LSSI_WRITE_A,
    REG_RF_CTRL,
    REG_RFE_PINMUX_A,
    REG_RRSR,
)
from wifit3.chips.rtl8821au.phy import (
    EfuseDefaults,
    _cfg_rf,
    load_agc_table,
    load_bb_table,
    load_mac_table,
    load_rf_a_table,
    phy_bb_config,
    phy_rf_config,
    post_mac_init_phy,
    switch_band_2g_20mhz,
)

from tests.chips.rtl8821au.test_mac_init import MockTransport


EFUSE = EfuseDefaults()


def test_basic_rates_value():
    assert BASIC_RATES_2G == 0x15F   # bits 0,1,2,3,4,6,8


def test_load_mac_table_dispatches_all_98_pairs():
    t = MockTransport()
    n = load_mac_table(t, EFUSE)
    assert n == 98
    # Every dispatch is write8.
    assert all(op == "w8" for (op, _, _) in t.writes)


def test_load_bb_table_dispatches_172_writes():
    t = MockTransport()
    n = load_bb_table(t, EFUSE)
    assert n == 172


def test_load_agc_table_dispatches_130_writes_for_rfe0():
    t = MockTransport()
    n = load_agc_table(t, EFUSE)
    assert n == 130


def test_load_rf_a_table_dispatches_279_writes_for_rfe0():
    t = MockTransport()
    n = load_rf_a_table(t, EFUSE)
    # 279 cfg ops + 2 delay markers (0xFFE) = 277 actual w32 ops + 2 sleeps.
    # Walker still dispatches the delay entries via _cfg_rf which just sleeps.
    assert n == 279
    # The two 0xFFE delays don't generate writes; everything else does.
    w32_writes = [w for w in t.writes if w[0] == "w32" and w[1] == REG_LSSI_WRITE_A]
    assert len(w32_writes) == 279 - 2


def test_cfg_rf_sipi_encoding():
    t = MockTransport()
    _cfg_rf(t, 0x18, 0x0001712A)
    # data_and_addr = ((0x18 << 20) | (0x0001712A & 0xFFFFF)) & 0x0FFFFFFF
    expected = (0x18 << 20) | 0x1712A
    assert t.writes == [("w32", REG_LSSI_WRITE_A, expected)]


def test_cfg_rf_addr_masked_to_byte():
    """RF addresses can exceed 0xFF in the source (e.g., 0xFFE delay); the
    SIPI encoding only takes the low 8 bits of addr."""
    t = MockTransport()
    _cfg_rf(t, 0x186, 0x000ABCDE)
    # Hardware sees addr & 0xff = 0x86
    expected = (0x86 << 20) | 0xABCDE
    assert t.writes == [("w32", REG_LSSI_WRITE_A, expected)]


def test_phy_bb_config_starts_with_sys_func_en_then_rf_ctrl():
    t = MockTransport()
    phy_bb_config(t, EFUSE)
    # First two writes should target REG_SYS_FUNC_EN
    assert t.writes[0][:2] == ("w8", 2)   # REG_SYS_FUNC_EN = 0x0002
    assert t.writes[1][:2] == ("w8", 2)
    # Then REG_RF_CTRL with BIT_RF_EN|BIT_RF_RSTB|BIT_RF_SDM_RSTB = 0x07
    rf_ctrl = [w for w in t.writes if w[1] == REG_RF_CTRL]
    assert rf_ctrl[0] == ("w8", REG_RF_CTRL, BIT_RF_EN | BIT_RF_RSTB | BIT_RF_SDM_RSTB)


def test_switch_band_2g_sets_basic_rates():
    t = MockTransport()
    # Seed REG_RRSR upper bits to verify mask preserves them.
    t._store(REG_RRSR, [0, 0, 0xAB, 0xCD])
    switch_band_2g_20mhz(t, EFUSE)
    rrsr = t._load(REG_RRSR, 4)
    assert rrsr & 0xFFFFF == BASIC_RATES_2G
    assert (rrsr >> 20) == 0xCDA   # upper 12 bits preserved (bits 28-20 of CDAB0000)


def test_switch_band_2g_skips_ext_lna_2g_when_efuse_clear():
    """ext_lna_2g=0 → bypass-LNA branch writes 0x7 to PINMUX low bits."""
    t = MockTransport()
    switch_band_2g_20mhz(t, EFUSE)
    pinmux_writes = [w for w in t.writes if w[1] == REG_RFE_PINMUX_A]
    # Among the PINMUX writes there must be one setting low 3 bits to 0x7.
    saw_bypass = False
    for op, _, val in pinmux_writes:
        if op == "w32" and (val & 0x07) == 0x7:
            saw_bypass = True
    assert saw_bypass, "expected the bypass-LNA pinmux write"


def test_post_mac_init_phy_end_to_end_op_count():
    """Smoke check that the orchestrator runs without raising and produces
    a substantial number of writes (>500)."""
    t = MockTransport()
    post_mac_init_phy(t, EFUSE)
    # 98 (mac) + 172 (bb) + 130 (agc) + 277 (rf) = 677 table-driven writes,
    # plus ~30 inline pokes, plus phy_bb_config's pre-table pokes.
    assert len(t.writes) > 600
    assert len(t.writes) < 900
