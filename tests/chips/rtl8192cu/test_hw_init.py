"""rtl92cu_hw_init: power-on, LLT, firmware, table selection per variant, IQK, LCK, PA bias, dm_init."""
import errno
import time
from importlib.resources import files

import pytest

from wifit3.chips.rtl8192cu import constants as C
from wifit3.chips.rtl8192cu import table
from wifit3.chips.rtl8192cu.core import rtl_fw_cb
from wifit3.chips.rtl8192cu.dm_common import rtl92c_dm_check_txpower_tracking
from wifit3.chips.rtl8192cu.hw import _rtl92cu_phy_param_tab_init, rtl92cu_hw_init
from wifit3.chips.rtl8192cu.phy import rtl92cu_phy_set_rf_reg
from wifit3.chips.rtl8192cu.phy_common import _rtl92c_phy_simularity_compare, rtl92c_phy_set_bb_reg
from wifit3.chips.rtl8192cu.priv import RtlPriv
from wifit3.chips.rtl8192cu.usb import UsbEndpointDescriptor, rtl_usb_probe
from wifit3.chips.rtl8192cu.wifi import calculate_bit_shift

from .fake import NORMAL_SIE_EP_2OUT, SYS_CFG_88C_TSMC, FakeChip, probed_efuse

BULK, INTR = 0x02, 0x03
EP_2OUT = [UsbEndpointDescriptor(0x81, BULK), UsbEndpointDescriptor(0x02, BULK),
           UsbEndpointDescriptor(0x03, BULK), UsbEndpointDescriptor(0x84, INTR)]
LSSI_A, LSSI_B = C.RFPGA0_XA_LSSIPARAMETER, C.RFPGA0_XB_LSSIPARAMETER


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)


def _probed(sys_cfg=SYS_CFG_88C_TSMC, pa_setting=0xFF, regs=None, **logical) -> tuple[RtlPriv, FakeChip]:
    io = FakeChip({C.REG_SYS_CFG: sys_cfg, C.REG_9346CR: C.EEPROM_EN, **NORMAL_SIE_EP_2OUT, **(regs or {})},
                  probed_efuse(pa_setting, **logical))
    priv = RtlPriv(io)
    rtl_usb_probe(priv, EP_2OUT, True)
    io.log.clear()
    return priv, io


def _rf_writes(io: FakeChip, lssi: int) -> list[tuple[int, int]]:
    return [((v >> 20) & 0x3F, v & 0xFFFFF) for v in io.writes(lssi)]


def _payload(name: str) -> bytes:
    return files("wifit3.chips.rtl8192cu").joinpath("assets").joinpath(name).read_bytes()[C.FW_HEADER_SIZE:]


def test_88c_hw_init_runs_every_stage():
    priv, io = _probed()
    assert rtl92cu_hw_init(priv) == 0
    hal, phy = priv.rtlhal, priv.phy
    assert hal.hw_type == C.HARDWARE_TYPE_RTL8192CU and hal.fw_ready
    assert hal.fw_version == 88 and hal.fw_subversion == 2
    assert b"".join(data for _addr, data in io.chunks) == _payload("rtl8192cufw_TMSC.bin")
    assert io.writes(C.REG_MCUFWDL + 2) == [0, 1, 2, 3]
    assert len(io.writes(C.REG_LLT_INIT)) == C.LLT_LAST_ENTRY_OF_TX_PKT_BUFFER + 1
    assert io.writes(C.REG_RQPN) == [C._HPQ(8 + 1) | C._PUBQ(C.CHIP_B_PAGE_NUM_PUBQ) | C.LD_RQPN]
    assert io.writes(C.REG_RQPN_NPQ) == [0x08]
    assert io.writes(C.REG_HIMR) == [0xFFFFFFFF]
    assert io.writes(C.REG_SECCFG) == [C.SCR_TXENCENABLE | C.SCR_RXDECENABLE | C.SCR_RXBCUSEDK | C.SCR_TXBCUSEDK]
    assert io.writes(C.REG_CAMCMD) == [0xC0000000]
    assert io.writes(C.REG_BCNTCFG) == [0x660F]
    assert phy.hwparam_tables[C.PHY_REG_1T] is table.RTL8192CUPHY_REG_1TARRAY
    assert phy.num_total_rfpath == 1 and io.writes(LSSI_B) == [0x00000000]  # the PHY_REG_1T row only
    assert 0x03a05600 in io.writes(0xc04) and phy.iqk_initialized
    assert (C.RF_CHNLBW, 0x08000) in _rf_writes(io, LSSI_A)
    assert io.writes(C.REG_TXPAUSE)[-2:] == [0xFF, 0x00]
    assert priv.dm.txpower_tracking and priv.dm_digtable.cur_igvalue == 0x20
    assert priv.dm.dm_flag == C.DYNAMIC_FUNC_DIG | C.HAL_DM_HIPWR_DISABLE | C.DYNAMIC_FUNC_SS


def test_set_bw_mode_is_a_no_op_while_the_hal_is_stopped():
    priv, io = _probed()
    rtl92cu_hw_init(priv)
    assert C.REG_BWOPMODE not in io.addrs("R")
    assert priv.phy.set_bwmode_inprogress is False


@pytest.mark.parametrize("pa_setting, path_a, pa_reg", [(0xFF, False, False), (0xEE, True, True)])
def test_initpabias_reads_efuse_0x1fa(pa_setting, path_a, pa_reg):
    priv, io = _probed(pa_setting=pa_setting, regs={0x16: 0x5A})
    rtl92cu_hw_init(priv)
    pa_bias = [(0x15, v) for v in (0x0F406, 0x4F406, 0x8F406, 0xCF406)]
    assert (_rf_writes(io, LSSI_A)[-4:] == pa_bias) == path_a
    assert io.writes(0x16) == ([0x9A] if pa_reg else [])


def test_92c_uses_2t_tables_and_configures_path_b():
    priv, io = _probed(sys_cfg=C.TYPE_ID, pa_setting=0xFC)
    assert rtl92cu_hw_init(priv) == 0
    phy = priv.phy
    assert priv.rtlhal.version == C.VERSION_NORMAL_TSMC_CHIP_92C and phy.num_total_rfpath == 2
    rf_b = _rf_writes(io, LSSI_B)
    table_b = list(zip(table.RTL8192CU_RADIOB_2TARRAY[::2], table.RTL8192CU_RADIOB_2TARRAY[1::2]))
    expected = [(a & 0x3F, v) for a, v in table_b]
    assert any(rf_b[i:i + len(expected)] == expected for i in range(len(rf_b)))
    assert rf_b[-4:] == [(0x15, v) for v in (0x0F406, 0x4F406, 0x8F406, 0xCF406)]
    assert 0x00010000 in io.writes(0x844)
    assert io.writes(C.REG_AMPDU_MIN_SPACE) == [C.MAX_MSS_DENSITY_2T]


def test_umc_a_cut_88c_loads_fw_a_and_patches_rx_gain():
    priv, io = _probed(sys_cfg=C.VENDOR_ID)
    assert rtl92cu_hw_init(priv) == 0
    assert b"".join(data for _a, data in io.chunks) == _payload("rtl8192cufw_A.bin")
    rf_a = _rf_writes(io, LSSI_A)
    assert (C.RF_RX_G1, 0x30255) in rf_a and (C.RF_RX_G2, 0x50a00) in rf_a


def test_high_pa_board_selects_hp_tables():
    priv = RtlPriv(FakeChip())
    priv.efuse.board_type = C.BOARD_USB_HIGH_PA
    _rtl92cu_phy_param_tab_init(priv)
    tables = priv.phy.hwparam_tables
    assert tables[C.PHY_REG_PG] is table.RTL8192CUPHY_REG_ARRAY_PG_HP
    assert tables[C.PHY_REG_1T] is table.RTL8192CUPHY_REG_1T_HPARRAY
    assert tables[C.RADIOA_1T] is table.RTL8192CURADIOA_1T_HPARRAY
    assert tables[C.AGCTAB_1T] is table.RTL8192CUAGCTAB_1T_HPARRAY
    assert tables[C.PHY_REG_2T] is table.RTL8192CUPHY_REG_2TARRAY


def test_test_chip_uses_chip_a_pages_and_block_firmware_write():
    io = FakeChip({C.REG_SYS_CFG: C.TRP_VAUX_EN, C.REG_TEST_SIE_OPTIONAL: 0, C.REG_TEST_USB_TXQS: 0x11})
    priv = RtlPriv(io)
    rtl_usb_probe(priv, EP_2OUT, True)
    io.log.clear()
    assert rtl92cu_hw_init(priv) == 0
    assert C.REG_RQPN_NPQ not in io.addrs("W")
    assert io.writes(C.REG_TRXDMA_CTRL + 1) == [C.HQSEL_VOQ | C.HQSEL_VIQ | C.HQSEL_MGTQ | C.HQSEL_HIQ]
    assert io.writes(C.REG_BCNTCFG) == [0x66FF]
    assert C.REG_MCUFWDL + 2 not in io.addrs("W")
    assert C.REG_SECCFG in io.addrs("W") and io.writes(C.REG_SECCFG) == [C.SCR_TXENCENABLE | C.SCR_RXDECENABLE]


def test_missing_firmware_leaves_an_empty_image():
    priv = RtlPriv(FakeChip())
    priv.max_fw_size = C.MAX_FW_SIZE
    priv.rtlhal.pfirmware = bytes(C.MAX_FW_SIZE)
    rtl_fw_cb(priv, None)
    assert priv.max_fw_size == 0 and priv.rtlhal.fwsize == 0
    priv.max_fw_size = 16
    rtl_fw_cb(priv, bytes(17))
    assert priv.rtlhal.fwsize == 0


def test_hw_init_without_firmware_buffer_returns_1():
    priv, _io = _probed()
    priv.rtlhal.pfirmware = None
    assert rtl92cu_hw_init(priv) == 1
    assert priv.rtlhal.fw_ready is False


def test_power_on_gives_up_without_autoload_done():
    priv, io = _probed(regs={C.REG_APS_FSMCO: 0})
    assert rtl92cu_hw_init(priv) == -errno.ENODEV
    assert io.addrs("R") == [C.REG_APS_FSMCO] * 102


def test_second_hw_init_triggers_the_thermal_meter():
    priv, io = _probed()
    rtl92cu_hw_init(priv)
    io.log.clear()
    rtl92cu_hw_init(priv)
    assert (C.RF_T_METER, 0x60) in _rf_writes(io, LSSI_A)
    assert priv.dm.tm_trigger == 1


def test_masked_bb_and_rf_writes_fold_unmasked_data():
    io = FakeChip({0x900: 0xFFFFFFFF})
    priv = RtlPriv(io)
    rtl92c_phy_set_bb_reg(priv, 0x900, 0x3FF, 0x1234)
    assert io.writes(0x900) == [0xFFFFFFFF & ~0x3FF | 0x1234]
    priv.phy.phyreg_def[0].rf3wire_offset = LSSI_A
    priv.phy.phyreg_def[0].rfhssi_para2 = C.RFPGA0_XA_HSSIPARAMETER2
    priv.phy.phyreg_def[0].rf_rb = C.RFPGA0_XA_LSSIREADBACK
    io.regs[C.RFPGA0_XA_LSSIREADBACK] = 0x00ABC
    rtl92cu_phy_set_rf_reg(priv, C.RF90_PATH_A, 0x18, C.MASK12BITS, 0x08001)
    assert _rf_writes(io, LSSI_A) == [(0x18, 0x08001)]
    assert calculate_bit_shift(0xF0000000) == 28 and calculate_bit_shift(0) == 0


@pytest.mark.parametrize("c1_row, c2_row, expected, row3", [
    ([100, 200, 300, 400], [100, 200, 300, 404], True, [0, 0, 0, 0]),
    ([100, 200, 0, 0], [100, 200, 300, 3], False, [100, 200, 0, 0]),
    ([100, 200, 300, 400], [110, 200, 300, 400], False, [0, 0, 0, 0]),
])
def test_simularity_compare(c1_row, c2_row, expected, row3):
    priv = RtlPriv(FakeChip())
    priv.rtlhal.version = C.VERSION_NORMAL_TSMC_CHIP_88C
    result = [c1_row + [0] * 4, [0] * 8, c2_row + [0] * 4, [0] * 8]
    assert _rtl92c_phy_simularity_compare(priv, result, 0, 2) is expected
    assert result[3][:4] == row3


def test_thermal_meter_callback_tracks_cck_swing():
    priv, io = _probed(regs={C.TRANSCEIVEA_HSPI_READBACK: 0x14})
    rtl92cu_hw_init(priv)
    priv.dm.txpower_track_control = True
    io.log.clear()
    rtl92c_dm_check_txpower_tracking(priv)
    assert priv.dm.tm_trigger == 1 and (C.RF_T_METER, 0x60) in _rf_writes(io, LSSI_A)
    rtl92c_dm_check_txpower_tracking(priv)
    assert priv.dm.tm_trigger == 0 and priv.dm.thermalvalue == 0x14
    assert [a for a in io.addrs("W") if 0xa22 <= a <= 0xa29] == list(range(0xa22, 0xa2a))
