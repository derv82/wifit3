"""PHY TX-power / baseband-block / crystal-cap behaviour (no RF tables re-checked)."""
from __future__ import annotations

import pytest

from wifit3.chips.rtl8192eu.constants import (
    FPGA_RF_MODE_CCK,
    FPGA_RF_MODE_OFDM,
    REG_FPGA0_RF_MODE,
    REG_TX_AGC_A_CCK1_MCS32,
    REG_TX_AGC_A_MCS03_MCS00,
    REG_TX_AGC_A_MCS15_MCS12,
    REG_TX_AGC_A_RATE18_06,
    REG_TX_AGC_B_CCK11_A_CCK2_11,
    REG_TX_AGC_B_CCK1_55_MCS32,
    REG_TX_AGC_B_MCS03_MCS00,
    REG_TX_AGC_B_MCS15_MCS12,
    REG_TX_AGC_B_RATE18_06,
)
from wifit3.chips.rtl8192eu.efuse import EfuseDefaults, PathPowerDiff
from wifit3.chips.rtl8192eu.phy import (
    channel_to_group_8192e,
    enable_cck_ofdm_block,
    set_crystal_cap,
    set_tx_power,
)

from tests.chips.rtl8192eu._mock import MockTransport

REG_MAC_PHY_CTRL = 0x002C


@pytest.mark.parametrize(
    ("channel", "group"),
    [(1, 0), (2, 0), (3, 1), (5, 1), (6, 2), (8, 2), (9, 3), (11, 3), (12, 4), (14, 4)],
)
def test_channel_to_group_8192e(channel, group):
    assert channel_to_group_8192e(channel) == group


def test_cck_ofdm_block_sets_fpga_rf_mode_bits_24_and_25():
    t = MockTransport()
    t.seed(REG_FPGA0_RF_MODE, 0x00000001)

    enable_cck_ofdm_block(t)

    assert FPGA_RF_MODE_CCK == 1 << 24
    assert FPGA_RF_MODE_OFDM == 1 << 25
    assert t.last_write_to(REG_FPGA0_RF_MODE) == 0x03000001


def test_crystal_cap_writes_both_xtal_fields_and_skips_when_unchanged():
    t = MockTransport()
    set_crystal_cap(t, 0x2A)
    assert t.last_write_to(REG_MAC_PHY_CTRL) == (0x2A << 18) | (0x2A << 12)

    t2 = MockTransport()
    set_crystal_cap(t2, 0x2A, prev_cap=0x2A)     # kernel caches and skips
    assert t2.writes_to(REG_MAC_PHY_CTRL) == []


def test_set_tx_power_uses_the_default_power_for_both_paths():
    t = MockTransport()
    set_tx_power(t, channel=1, efuse=EfuseDefaults())

    # 0x22 replicated across the CCK/OFDM/MCS lanes on path A and path B.
    assert t.last_write_to(REG_TX_AGC_A_CCK1_MCS32) == 0x00002200
    assert t.last_write_to(REG_TX_AGC_B_CCK11_A_CCK2_11) == 0x22222222
    assert t.last_write_to(REG_TX_AGC_A_RATE18_06) == 0x22222222
    assert t.last_write_to(REG_TX_AGC_A_MCS03_MCS00) == 0x22222222
    assert t.last_write_to(REG_TX_AGC_A_MCS15_MCS12) == 0x22222222
    assert t.last_write_to(REG_TX_AGC_B_CCK1_55_MCS32) == 0x22222200
    assert t.last_write_to(REG_TX_AGC_B_RATE18_06) == 0x22222222
    assert t.last_write_to(REG_TX_AGC_B_MCS03_MCS00) == 0x22222222
    assert t.last_write_to(REG_TX_AGC_B_MCS15_MCS12) == 0x22222222


def test_set_tx_power_selects_the_channel_group_index():
    efuse = EfuseDefaults(
        cck_tx_power_index_A=(0x01, 0x02, 0x03, 0x04, 0x05, 0x06),
        cck_tx_power_index_B=(0x11, 0x12, 0x13, 0x14, 0x15, 0x16),
        ht40_1s_tx_power_index_A=(0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F),
        ht40_1s_tx_power_index_B=(0x1A, 0x1B, 0x1C, 0x1D, 0x1E, 0x1F),
    )
    t = MockTransport()

    set_tx_power(t, channel=6, efuse=efuse)      # group 2

    assert t.last_write_to(REG_TX_AGC_A_CCK1_MCS32) == 0x00000300
    assert t.last_write_to(REG_TX_AGC_B_CCK11_A_CCK2_11) == 0x03030313
    assert t.last_write_to(REG_TX_AGC_A_RATE18_06) == 0x0C0C0C0C
    assert t.last_write_to(REG_TX_AGC_B_CCK1_55_MCS32) == 0x13131300
    assert t.last_write_to(REG_TX_AGC_B_RATE18_06) == 0x1C1C1C1C


def test_set_tx_power_applies_the_per_path_diffs():
    efuse = EfuseDefaults(
        cck_tx_power_index_A=(0x10,) * 6,
        cck_tx_power_index_B=(0x10,) * 6,
        ht40_1s_tx_power_index_A=(0x20,) * 6,
        ht40_1s_tx_power_index_B=(0x20,) * 6,
        # Path A reads diff[0]; path B reads diff[1] (kernel tx_idx post-increment).
        ofdm_tx_power_diff=(PathPowerDiff(2, 2), PathPowerDiff(3, 3)) + (PathPowerDiff(0, 0),) * 2,
        ht20_tx_power_diff=(PathPowerDiff(1, 1), PathPowerDiff(4, 4)) + (PathPowerDiff(0, 0),) * 2,
    )
    t = MockTransport()

    set_tx_power(t, channel=1, efuse=efuse)

    # OFDM base A = 0x20 + 2 = 0x22; MCS base A = 0x20 + ht20 diff 1 = 0x21.
    assert t.last_write_to(REG_TX_AGC_A_RATE18_06) == 0x22222222
    assert t.last_write_to(REG_TX_AGC_A_MCS03_MCS00) == 0x21212121
    # Path B reads index 1: OFDM 0x20+3 = 0x23; MCS 0x20+4 = 0x24.
    assert t.last_write_to(REG_TX_AGC_B_RATE18_06) == 0x23232323
    assert t.last_write_to(REG_TX_AGC_B_MCS03_MCS00) == 0x24242424
