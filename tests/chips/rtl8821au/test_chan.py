"""Tests for set_channel(channel=1, 20MHz) on 2.4 GHz."""
from __future__ import annotations

import pytest

from wifit3.chips.rtl8821au.chan import (
    _switch_channel,
    read_rf,
    set_channel_2g_20mhz,
    write_rf_masked,
)
from wifit3.chips.rtl8821au.constants import (
    REG_3WIRE_SWA,
    REG_ADC160,
    REG_ADCCLK,
    REG_CLKTRK,
    REG_DATA_SC,
    REG_HSSI_READ,
    REG_L1PKTH,
    REG_LSSI_WRITE_A,
    REG_SI_READ_A,
    REG_WMAC_TRXPTCL_CTL,
    RF18_BW_MASK,
    RFREG_MASK,
    RF_CFGCH,
)

from tests.chips.rtl8821au.test_mac_init import MockTransport


def test_read_rf_path_si_mode():
    """pi_mode=0 → read from REG_SI_READ_A."""
    t = MockTransport()
    # Seed REG_3WIRE_SWA so BIT(2)=0 (SI mode).
    t._store(REG_3WIRE_SWA, [0x00, 0, 0, 0])
    # Seed the SI read register to a known value.
    t._store(REG_SI_READ_A, [0x34, 0x12, 0, 0])
    val = read_rf(t, addr=0x18, mask=RFREG_MASK)
    assert val == 0x1234
    # First write should be REG_HSSI_READ low byte = 0x18.
    assert any(w[1] == REG_HSSI_READ and (w[2] & 0xFF) == 0x18 for w in t.writes)


def test_write_rf_full_mask_skips_readback():
    """When mask == RFREG_MASK the kernel skips the read-back. We mirror."""
    t = MockTransport()
    write_rf_masked(t, 0x18, RFREG_MASK, 0x12345)
    # No read of REG_3WIRE_SWA / REG_HSSI_READ for read-back path.
    assert ("r32", REG_3WIRE_SWA) not in t.reads
    # One write to REG_LSSI_WRITE_A with encoded (addr<<20 | data).
    sipi_writes = [w for w in t.writes if w[1] == REG_LSSI_WRITE_A]
    assert len(sipi_writes) == 1
    assert sipi_writes[0][2] == (0x18 << 20) | 0x12345


def test_write_rf_masked_does_readback_then_merge():
    """Partial mask → read old value, merge masked bits, then write."""
    t = MockTransport()
    t._store(REG_3WIRE_SWA, [0x00, 0, 0, 0])     # SI mode
    t._store(REG_SI_READ_A, [0xAA, 0xBB, 0, 0])  # old = 0xBBAA, masked to RFREG=0xBBAA & 0xFFFFF=0xBBAA
    # Write 3 into bits 11:10 (RF18_BW_MASK=0xC00) keeping the rest.
    write_rf_masked(t, RF_CFGCH, RF18_BW_MASK, 3)
    sipi = [w for w in t.writes if w[1] == REG_LSSI_WRITE_A][-1]
    # Expected data field: (0xBBAA & ~0xC00) | (3 << 10) = 0xB3AA | 0xC00 = 0xBFAA
    # (0xBBAA & ~0xC00) = 0xB3AA; then | 0xC00 = 0xBFAA
    expected_data = (0xBBAA & ~0xC00) | (3 << 10)
    encoded = (0x18 << 20) | (expected_data & RFREG_MASK)
    assert sipi[2] == encoded


def test_switch_channel_sets_clktrk_and_two_cfgch_writes():
    t = MockTransport()
    t._store(REG_3WIRE_SWA, [0, 0, 0, 0])
    t._store(REG_SI_READ_A, [0, 0, 0, 0])
    _switch_channel(t, 1)
    # REG_CLKTRK masked write
    clktrk_writes = [w for w in t.writes if w[1] == REG_CLKTRK]
    assert len(clktrk_writes) == 1
    # 0x96a placed at shift 17 (lowest bit of 0x1ffe0000)
    expected = 0x96A << 17
    assert clktrk_writes[0][2] & 0x1FFE0000 == expected & 0x1FFE0000
    # Two SIPI writes to RF_CFGCH (band + channel)
    sipi = [w for w in t.writes if w[1] == REG_LSSI_WRITE_A]
    assert len(sipi) == 2


def test_set_channel_2g_20mhz_data_sc_zero_when_primary_zero():
    t = MockTransport()
    t._store(REG_3WIRE_SWA, [0, 0, 0, 0])
    set_channel_2g_20mhz(t, 1, primary_chan_idx=0)
    dsc = [w for w in t.writes if w[1] == REG_DATA_SC]
    assert dsc[-1][2] == 0


def test_set_channel_2g_20mhz_writes_expected_regs():
    t = MockTransport()
    t._store(REG_3WIRE_SWA, [0, 0, 0, 0])
    set_channel_2g_20mhz(t, 6)
    touched = {w[1] for w in t.writes}
    # Must touch: REG_CLKTRK, REG_LSSI_WRITE_A (SIPI), REG_WMAC_TRXPTCL_CTL,
    # REG_DATA_SC, REG_ADCCLK, REG_ADC160, REG_L1PKTH.
    for r in (
        REG_CLKTRK, REG_LSSI_WRITE_A, REG_WMAC_TRXPTCL_CTL,
        REG_DATA_SC, REG_ADCCLK, REG_ADC160, REG_L1PKTH,
    ):
        assert r in touched, f"missing write to 0x{r:04x}"


def test_set_channel_rejects_invalid_channel():
    t = MockTransport()
    with pytest.raises(ValueError):
        set_channel_2g_20mhz(t, 15)
    with pytest.raises(ValueError):
        set_channel_2g_20mhz(t, 0)


def test_set_channel_2g_20mhz_l1pkth_is_8_for_1t1r():
    t = MockTransport()
    t._store(REG_3WIRE_SWA, [0, 0, 0, 0])
    set_channel_2g_20mhz(t, 1)
    l1 = [w for w in t.writes if w[1] == REG_L1PKTH]
    assert l1[-1][2] & 0x03C00000 == (8 << 22)


# --- 5 GHz ---

from wifit3.chips.rtl8821au.chan import (  # noqa: E402
    _lookup_fc_area,
    _lookup_rf_mod_ag,
    channel_band_is_2g,
    set_channel_5g_20mhz,
    CHANNELS_5G_ALL,
)


def test_channel_band_is_2g_split():
    for ch in (1, 6, 11, 14):
        assert channel_band_is_2g(ch) is True
    for ch in (36, 48, 100, 165):
        assert channel_band_is_2g(ch) is False


def test_lookup_fc_area_branches():
    # rtw88xxa.c:1330..1346
    assert _lookup_fc_area(1) == 0x96A
    assert _lookup_fc_area(36) == 0x494
    assert _lookup_fc_area(48) == 0x494
    assert _lookup_fc_area(52) == 0x453
    assert _lookup_fc_area(100) == 0x452
    assert _lookup_fc_area(118) == 0x412
    assert _lookup_fc_area(165) == 0x412


def test_lookup_rf_mod_ag_branches():
    assert _lookup_rf_mod_ag(1) == 0x000
    assert _lookup_rf_mod_ag(36) == 0x101
    assert _lookup_rf_mod_ag(64) == 0x101
    assert _lookup_rf_mod_ag(100) == 0x301
    assert _lookup_rf_mod_ag(140) == 0x301
    assert _lookup_rf_mod_ag(149) == 0x501


def test_set_channel_5g_uses_correct_fc_area():
    t = MockTransport()
    t._store(REG_3WIRE_SWA, [0, 0, 0, 0])
    set_channel_5g_20mhz(t, 36)
    clktrk = [w for w in t.writes if w[1] == REG_CLKTRK][-1]
    # fc_area=0x494 placed at shift 17 (lowest bit of 0x1ffe0000)
    assert (clktrk[2] >> 17) & 0xFFF == 0x494


def test_set_channel_5g_rejects_invalid_channel():
    import pytest as _pytest
    t = MockTransport()
    t._store(REG_3WIRE_SWA, [0, 0, 0, 0])
    with _pytest.raises(ValueError):
        set_channel_5g_20mhz(t, 33)


def test_channels_5g_disjoint_from_24g():
    assert 1 not in CHANNELS_5G_ALL
    assert 36 in CHANNELS_5G_ALL
    assert 165 in CHANNELS_5G_ALL
