"""rtl8192cu probe: chip version, EFUSE map + parse, endpoint config, firmware selection."""
import pytest

from wifit3.chips.rtl8192cu import SUPPORTED_IDS, import_driver
from wifit3.chips.rtl8192cu import constants as C
from wifit3.chips.rtl8192cu.efuse import efuse_power_switch, read_efuse
from wifit3.chips.rtl8192cu.mac import rtl92c_read_chip_version
from wifit3.chips.rtl8192cu.priv import RtlPriv
from wifit3.chips.rtl8192cu.usb import ProbeError, UsbEndpointDescriptor, rtl_usb_probe

from .fake import FAKE_MAC, NORMAL_SIE_EP_2OUT, SYS_CFG_88C_TSMC, FakeRegisters, encode_physical, logical_map

BULK, INTR = 0x02, 0x03
EP_2OUT = [UsbEndpointDescriptor(0x81, BULK), UsbEndpointDescriptor(0x02, BULK),
           UsbEndpointDescriptor(0x03, BULK), UsbEndpointDescriptor(0x84, INTR)]
EP_3OUT = EP_2OUT[:3] + [UsbEndpointDescriptor(0x04, BULK), UsbEndpointDescriptor(0x85, INTR)]
EP_1OUT = [UsbEndpointDescriptor(0x81, BULK), UsbEndpointDescriptor(0x02, BULK)]


def _priv(sys_cfg: int, hpon_fsm: int = 0, regs: dict | None = None, efuse=None) -> tuple[RtlPriv, FakeRegisters]:
    io = FakeRegisters({C.REG_SYS_CFG: sys_cfg, C.REG_HPON_FSM: hpon_fsm, **(regs or {})}, efuse)
    return RtlPriv(io), io


@pytest.mark.parametrize("sys_cfg, hpon_fsm, version, rf_type, reads_hpon", [
    (SYS_CFG_88C_TSMC, 0, C.VERSION_NORMAL_TSMC_CHIP_88C, C.RF_1T1R, False),
    (C.TYPE_ID, 0, C.VERSION_NORMAL_TSMC_CHIP_92C, C.RF_2T2R, True),
    # IS_92C_SERIAL is false for RF type bits 0b11, so the kernel reports a 1T2R chip as 1T1R.
    (C.TYPE_ID, 1 << 22, C.VERSION_NORMAL_TSMC_CHIP_92C_1T2R, C.RF_1T1R, True),
    (C.VENDOR_ID, 0, C.VERSION_NORMAL_UMC_CHIP_88C_A_CUT, C.RF_1T1R, False),
    (C.VENDOR_ID | 0x1000, 0, C.VERSION_NORMAL_UMC_CHIP_88C_B_CUT, C.RF_1T1R, False),
    (C.VENDOR_ID | C.TYPE_ID | 0x1000, 0, C.VERSION_NORMAL_UMC_CHIP_92C_B_CUT, C.RF_2T2R, True),
    (C.TRP_VAUX_EN, 0, C.VERSION_TEST_CHIP_88C, C.RF_1T1R, False),
    (C.TRP_VAUX_EN | C.TYPE_ID, 0, C.VERSION_TEST_CHIP_92C, C.RF_2T2R, False),
])
def test_read_chip_version(sys_cfg, hpon_fsm, version, rf_type, reads_hpon):
    priv, io = _priv(sys_cfg, hpon_fsm)
    rtl92c_read_chip_version(priv)
    assert priv.rtlhal.version == version
    assert priv.phy.rf_type == rf_type
    assert (C.REG_HPON_FSM in io.addrs("R")) == reads_hpon
    assert priv.dm.rfpath_rxenable[1] == (rf_type != C.RF_1T1R)


def test_efuse_power_switch_at_probe_writes_0x69_to_register_0():
    priv, io = _priv(0, regs={C.REG_SYS_FUNC_EN: 0x0000, C.REG_SYS_CLKR: 0x0000})
    efuse_power_switch(priv, False, True)
    efuse_power_switch(priv, False, False)
    assert io.log == [
        ("W", 0x0000, 1, 0x69),
        ("R", C.REG_SYS_FUNC_EN, 2, 0x0000), ("W", C.REG_SYS_FUNC_EN, 2, C.FEN_ELDR),
        ("R", C.REG_SYS_CLKR, 2, 0x0000), ("W", C.REG_SYS_CLKR, 2, C.LOADER_CLK_EN | C.EFUSE_ANA8M),
        ("W", 0x0000, 1, 0x00),
    ]


def test_read_efuse_decodes_normal_and_extended_headers():
    logical = logical_map()
    priv, _io = _priv(0, efuse=encode_physical(logical, extended_sections=(2, 15)))
    out = bytearray(C.HWSET_MAX_SIZE)
    read_efuse(priv, 0, C.HWSET_MAX_SIZE, out)
    assert out == logical


def _efuse_priv(logical, ep_regs=NORMAL_SIE_EP_2OUT, sys_cfg=SYS_CFG_88C_TSMC, eeprom_cr=C.EEPROM_EN):
    regs = {C.REG_9346CR: eeprom_cr, C.REG_SYS_FUNC_EN: 0x541C, C.REG_SYS_CLKR: 0x70A3, **ep_regs}
    return _priv(sys_cfg, regs=regs, efuse=encode_physical(logical))


def test_probe_88cu_parses_efuse_and_maps_two_endpoints():
    priv, io = _efuse_priv(logical_map())
    rtl_usb_probe(priv, EP_2OUT, True)
    efuse, usb = priv.efuse, priv.usb
    assert efuse.autoload_failflag is False
    assert efuse.dev_addr == FAKE_MAC
    assert (efuse.eeprom_vid, efuse.eeprom_did) == (0x0BDA, 0x8176)
    assert efuse.eeprom_channelplan == 0x0A and efuse.eeprom_version == 0x03
    assert efuse.txpwrlevel_cck[0][:4] == [0x20, 0x20, 0x20, 0x21]
    assert efuse.txpwrlevel_ht40_2s[0][0] == 0x1A - 0x1 and efuse.txpwrlevel_ht40_2s[1][0] == 0x02 - 0x2
    assert efuse.txpwr_ht20diff[0][0] == -1 and efuse.txpwr_ht20diff[1][0] == -8
    assert efuse.legacy_ht_txpowerdiff == 0 and efuse.txpwr_legacyhtdiff[1][13] == 0x5
    assert efuse.pwrgroup_ht40[0][0] == 0x2 and efuse.pwrgroup_ht20[1][0] == 0x3
    assert efuse.eeprom_tssi == [0x11, 0x22] and efuse.eeprom_thermalmeter == 0x0C
    assert efuse.board_type == C.BOARD_USB_DONGLE and efuse.external_pa == 0
    assert usb.out_eps[:2] == [2, 3] and usb.in_ep == 1 and usb.in_ep_nums == 2
    assert usb.out_queue_sel == C.TX_SELE_HQ | C.TX_SELE_NQ
    assert usb.ep_mapping[C.RTL_TXQ_BE] == 3 and usb.ep_mapping[C.RTL_TXQ_MGT] == 2
    assert usb.max_bulk_out_size == C.USB_HIGH_SPEED_BULK_SIZE
    assert priv.rtlhal.fw_name == "rtl8192cufw_TMSC.bin"
    assert io.addrs("R")[-2:] == [C.REG_NORMAL_SIE_EP + 1, C.REG_NORMAL_SIE_EP + 2]


def test_probe_high_pa_board_and_hp_oem():
    logical = logical_map(svid=0x103C, smid=0x1629, rf_opt1=C.BOARD_USB_HIGH_PA << 5, thermal=0x1F)
    priv, _io = _efuse_priv(logical)
    rtl_usb_probe(priv, EP_2OUT, False)
    assert priv.efuse.board_type == C.BOARD_USB_HIGH_PA and priv.efuse.external_pa == 1
    assert priv.rtlhal.oem_id == C.RT_CID_819X_HP and priv.ledctl.led_opendrain is True
    assert priv.efuse.eeprom_thermalmeter == 0x12
    assert priv.usb.max_bulk_out_size == C.USB_FULL_SPEED_BULK_SIZE


@pytest.mark.parametrize("oemid, oem_id", [
    (C.EEPROM_CID_TOSHIBA, C.RT_CID_TOSHIBA), (C.EEPROM_CID_QMI, C.RT_CID_819X_QMI),
    (C.EEPROM_CID_WHQL, C.RT_CID_DEFAULT),
])
def test_probe_oem_id(oemid, oem_id):
    priv, _io = _efuse_priv(logical_map(oemid=oemid))
    rtl_usb_probe(priv, EP_2OUT, True)
    assert priv.rtlhal.oem_id == oem_id


def test_probe_92cu_three_endpoints_maps_with_wmm():
    regs = {C.REG_NORMAL_SIE_EP + 1: 0x11, C.REG_NORMAL_SIE_EP + 2: 0x01}
    priv, _io = _efuse_priv(logical_map(did=0x8178), ep_regs=regs, sys_cfg=C.TYPE_ID)
    rtl_usb_probe(priv, EP_3OUT, True)
    usb = priv.usb
    assert usb.out_queue_sel == C.TX_SELE_HQ | C.TX_SELE_NQ | C.TX_SELE_LQ
    assert usb.ep_mapping[C.RTL_TXQ_BE] == 4 and usb.ep_mapping[C.RTL_TXQ_BK] == 3
    assert usb.ep_mapping[C.RTL_TXQ_VO] == 2


def test_probe_one_endpoint():
    regs = {C.REG_NORMAL_SIE_EP + 1: 0x01, C.REG_NORMAL_SIE_EP + 2: 0x00}
    priv, _io = _efuse_priv(logical_map(), ep_regs=regs)
    rtl_usb_probe(priv, EP_1OUT, True)
    assert set(priv.usb.ep_mapping) == {2}


def test_probe_endpoint_count_mismatch_fails():
    priv, _io = _efuse_priv(logical_map())
    with pytest.raises(ProbeError):
        rtl_usb_probe(priv, EP_1OUT, True)


def test_probe_autoload_fail_skips_efuse_read_and_parse():
    priv, io = _efuse_priv(logical_map(), eeprom_cr=0x00)
    rtl_usb_probe(priv, EP_2OUT, True)
    assert priv.efuse.autoload_failflag is True
    assert C.REG_EFUSE_CTRL not in io.addrs("R")
    assert priv.efuse.dev_addr == bytes(6)
    assert priv.efuse.efuse_map[C.EFUSE_INIT_MAP][:C.HWSET_MAX_SIZE] == b"\xff" * C.HWSET_MAX_SIZE


def test_probe_boot_from_eeprom_skips_efuse():
    priv, io = _efuse_priv(logical_map(), eeprom_cr=C.EEPROM_EN | C.BOOT_FROM_EEPROM)
    rtl_usb_probe(priv, EP_2OUT, True)
    assert priv.efuse.epromtype == C.EEPROM_93C46
    assert 0x0000 not in io.addrs("W")


def test_probe_bad_eeprom_id_marks_autoload_fail():
    logical = logical_map()
    logical[0:2] = b"\x00\x00"
    priv, _io = _efuse_priv(logical)
    rtl_usb_probe(priv, EP_2OUT, True)
    assert priv.efuse.autoload_failflag is True
    assert priv.efuse.dev_addr == bytes(6)


def test_test_chip_skips_eeprom_and_uses_test_endpoint_config():
    regs = {C.REG_TEST_SIE_OPTIONAL: 0x20, C.REG_TEST_USB_TXQS: 0xF0}
    priv, io = _priv(C.TRP_VAUX_EN, regs=regs)
    rtl_usb_probe(priv, EP_1OUT, True)
    assert C.REG_9346CR not in io.addrs("R")
    assert io.addrs("R") == [C.REG_SYS_CFG, C.REG_TEST_SIE_OPTIONAL, C.REG_TEST_USB_TXQS]
    assert priv.usb.out_queue_sel == C.TX_SELE_LQ


@pytest.mark.parametrize("sys_cfg, fw_name", [
    (SYS_CFG_88C_TSMC, "rtl8192cufw_TMSC.bin"),
    (C.VENDOR_ID, "rtl8192cufw_A.bin"),
    (C.VENDOR_ID | C.TYPE_ID, "rtl8192cufw_TMSC.bin"),
    (C.VENDOR_ID | 0x1000, "rtl8192cufw_B.bin"),
])
def test_firmware_selection(sys_cfg, fw_name):
    priv, _io = _efuse_priv(logical_map(), sys_cfg=sys_cfg)
    endpoints = EP_2OUT
    rtl_usb_probe(priv, endpoints, True)
    assert priv.rtlhal.fw_name == fw_name


def test_supported_ids_and_lazy_driver():
    assert any((d.vid, d.pid) == (0x0BDA, 0x8176) for d in SUPPORTED_IDS)
    assert len({(d.vid, d.pid) for d in SUPPORTED_IDS}) == len(SUPPORTED_IDS) == 72
    assert import_driver().__name__ == "RTL8192CUDriver"
