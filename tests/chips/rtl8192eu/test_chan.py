"""Channel tune: rtl8xxxu_gen2_config_channel (2.4 GHz / 20 MHz), SIPI access.

Both RF paths are asserted exactly (8192eu is 2T2R).
"""
from __future__ import annotations

import pytest

from wifit3.chips.rtl8192eu.chan import (
    REG_DATA_SUBCHANNEL,
    REG_R2T_SIFS,
    REG_SIFS_CCK,
    REG_SIFS_OFDM,
    REG_T2T_SIFS,
    read_rfreg,
    set_channel_2g_20mhz,
)
from wifit3.chips.rtl8192eu.constants import (
    MODE_AG_BW_20MHZ_8723B,
    MODE_AG_CHANNEL_MASK,
    REG_FPGA0_RF_MODE,
    REG_FPGA0_XA_HSSI_PARM1,
    REG_FPGA0_XA_HSSI_PARM2,
    REG_FPGA0_XA_LSSI_PARM,
    REG_FPGA0_XA_LSSI_READBACK,
    REG_FPGA0_XB_LSSI_PARM,
    REG_FPGA1_RF_MODE,
    REG_HSPI_XA_READBACK,
    REG_OFDM0_TX_PSDO_NOISE_WEIGHT,
    REG_WMAC_TRXPTCL_CTL,
    RF6052_REG_MODE_AG,
)
from wifit3.chips.rtl8192eu.phy import (
    REG_FPGA0_POWER_SAVE,
    RF_A,
    RF_B,
    write_rfreg,
)

from tests.chips.rtl8192eu._mock import MockTransport


XA_SIPI = REG_FPGA0_XA_LSSI_PARM          # 0x0840
XB_SIPI = REG_FPGA0_XB_LSSI_PARM          # 0x0844


def _seeded_transport() -> MockTransport:
    t = MockTransport()
    t.seed(REG_WMAC_TRXPTCL_CTL, 0xFFFF, 2)      # BW field gets cleared, re-OR'd
    t.seed(REG_FPGA0_RF_MODE, 0x00000001)        # FPGA_RF_MODE bit set
    t.seed(REG_FPGA1_RF_MODE, 0x00000001)
    t.seed(REG_OFDM0_TX_PSDO_NOISE_WEIGHT, 0xC0000001)
    t.seed(REG_FPGA0_POWER_SAVE, 0x00020000)     # BIT(17) already set
    t.seed(REG_FPGA0_XA_HSSI_PARM2, 0)
    t.seed(REG_FPGA0_XA_HSSI_PARM1, 0)           # PI clear -> LSSI readback
    t.seed(REG_FPGA0_XA_LSSI_READBACK, 0x123)
    return t


# ---- SIPI read ----------------------------------------------------------


def test_read_rfreg_uses_the_lssi_readback_when_pi_is_clear():
    t = MockTransport()
    t.seed(REG_FPGA0_XA_HSSI_PARM1, 0)
    t.seed(REG_FPGA0_XA_LSSI_READBACK, 0x00ABCDEF)
    assert read_rfreg(t, RF_A, RF6052_REG_MODE_AG) == 0x00ABCDEF & 0xFFFFF


def test_read_rfreg_uses_the_hspi_readback_when_pi_is_set():
    t = MockTransport()
    t.seed(REG_FPGA0_XA_HSSI_PARM1, 1 << 8)
    t.seed(REG_HSPI_XA_READBACK, 0x00054321)
    assert read_rfreg(t, RF_A, RF6052_REG_MODE_AG) == 0x00054321


def test_read_rfreg_sets_the_address_and_edge_bits_on_hssi_parm2():
    t = MockTransport()
    t.seed(REG_FPGA0_XA_HSSI_PARM1, 0)
    read_rfreg(t, RF_A, 0x18)
    writes = [value for _op, _addr, value in t.writes_to(REG_FPGA0_XA_HSSI_PARM2)]
    # Snapshot clear-edge, then the transaction value (addr << 23 | EDGE_READ),
    # then the snapshot with EDGE_READ set again.
    assert writes[0] == 0
    assert writes[1] == (0x18 << 23) | (1 << 31)
    assert writes[2] == 1 << 31


# ---- SIPI write ---------------------------------------------------------


def test_write_rfreg_encodes_addr_20_and_uses_the_path_b_register():
    t = MockTransport()
    t.seed(REG_FPGA0_POWER_SAVE, 0x00020000)

    write_rfreg(t, RF_B, 0x18, 0x12345)

    assert t.writes_to(XB_SIPI) == [("w32", XB_SIPI, (0x18 << 20) | 0x12345)]
    # BIT(17) of REG_FPGA0_POWER_SAVE brackets the write: clear, then restore.
    assert [v for _op, _addr, v in t.writes_to(REG_FPGA0_POWER_SAVE)] == [
        0x00000000, 0x00020000,
    ]
    assert t.writes_to(XA_SIPI) == []


# ---- channel tune, path A ----------------------------------------------


def test_set_channel_2g_20mhz_writes_the_gen2_sequence_for_channel_6():
    t = _seeded_transport()
    set_channel_2g_20mhz(t, 6)

    # FPGA_RF_MODE: clear the low bit on both baseband blocks.
    assert t.last_write_to(REG_FPGA0_RF_MODE) == 0
    assert t.last_write_to(REG_FPGA1_RF_MODE) == 0
    # PSDO noise weight: clear BIT(30) | BIT(31).
    assert t.last_write_to(REG_OFDM0_TX_PSDO_NOISE_WEIGHT) == 1

    # BW field cleared, 20 MHz value OR'd back in (0 = BW20 in this encoding).
    assert t.last_write_to(REG_WMAC_TRXPTCL_CTL) == 0xFE7F
    assert t.last_write_to(REG_DATA_SUBCHANNEL) == 0

    # HT SIFS byte, R2T/T2T SIFS words.
    assert t.last_write_to(REG_SIFS_CCK + 1) == 0x0E
    assert t.last_write_to(REG_SIFS_OFDM + 1) == 0x0E
    assert t.last_write_to(REG_R2T_SIFS) == 0x0808
    assert t.last_write_to(REG_T2T_SIFS) == 0x0A0A

    # Two RF MODE_AG writes on path A: channel, then the 20 MHz BW field.
    sipi = [value for _op, _addr, value in t.writes_to(XA_SIPI)]
    assert sipi == [
        (0x18 << 20) | 6,
        (0x18 << 20) | ((0x123 & ~0xC00) | MODE_AG_BW_20MHZ_8723B),
    ]
    assert sipi[0] & MODE_AG_CHANNEL_MASK == 6
    assert sipi[1] & 0xC00 == MODE_AG_BW_20MHZ_8723B


@pytest.mark.parametrize("channel", [0, 15, -1, 200])
def test_set_channel_rejects_out_of_band_channels(channel):
    t = _seeded_transport()
    with pytest.raises(ValueError, match="channel must be in 1..14"):
        set_channel_2g_20mhz(t, channel)
    assert t.writes == []


def test_set_channel_tunes_every_2g_channel():
    for channel in range(1, 15):
        t = _seeded_transport()
        set_channel_2g_20mhz(t, channel)
        first = t.writes_to(XA_SIPI)[0][2]
        assert first == (0x18 << 20) | channel


def test_set_channel_tunes_path_b_too():
    """2T2R must tune both RF paths (8192eu `rf_paths = 2`, 8192e.c:490)."""
    t = _seeded_transport()
    set_channel_2g_20mhz(t, 6)

    path_b = [value for _op, _addr, value in t.writes_to(XB_SIPI)]
    assert path_b

    assert path_b[0] >> 20 == 0x18
    assert path_b[0] & MODE_AG_CHANNEL_MASK == 6
    assert path_b[-1] & 0xC00 == MODE_AG_BW_20MHZ_8723B
