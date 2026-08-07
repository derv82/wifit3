from types import SimpleNamespace
from unittest.mock import MagicMock

from wifit3.chips.rtl8822cu.constants import CHIP_ID_RTL8822CU
from wifit3.chips.rtl8822cu.chipid import read_chip_info
from wifit3.chips.rtl8822cu.efuse import decode_logical_map
from wifit3.chips.rtl8822cu.efuse import EfuseInfo
from wifit3.chips.rtl8822cu.firmware import load_firmware
from wifit3.chips.rtl8822cu.firmware import _fw_tx_desc, _upload_section
from wifit3.chips.rtl8822cu.power_seq import card_enable_flow_8822c
from wifit3.chips.rtl8822cu.mac import RCR_MONITOR, enable_bb_rf, enter_monitor_mode, init_rx_mac
from wifit3.chips.rtl8822cu.rx import iter_bulk_frames
from wifit3.chips.rtl8822cu.phy import _write_rf, initialize_phy, load_table, selected_writes
from wifit3.chips.rtl8822cu.driver import RTL8822CUDriver
from wifit3.chips.rtl8822cu.transport import EndpointLayout, RTL8822CUTransport
from wifit3.models.device_id import DeviceID


def test_identity_constant_matches_hardware_probe():
    assert CHIP_ID_RTL8822CU == 0x13


def test_endpoint_layout_is_explicit_and_ordered():
    layout = EndpointLayout(0, (0x84, 0x87), (0x05, 0x06, 0x08))
    assert layout.bulk_in == (0x84, 0x87)
    assert layout.bulk_out == (0x05, 0x06, 0x08)


def test_driver_can_be_constructed_from_usb_device():
    dev = MagicMock()
    entry = DeviceID(0x2357, 0x0137, "RTL8822CU")
    driver = RTL8822CUDriver.from_usb_device(dev, entry)
    assert driver.transport.dev is dev
    assert driver.FAKE_MAC.value == "spoofable"


def test_runtime_retune_replays_phy_tables_before_channel_write(mocker):
    driver = RTL8822CUDriver.from_usb_device(MagicMock(), DeviceID(0x2357, 0x0137, "RTL8822CU"))
    driver.chip_info = SimpleNamespace(cut=5)
    driver.efuse = SimpleNamespace(rfe_type=3)
    init = mocker.patch("wifit3.chips.rtl8822cu.driver.initialize_phy")
    tune = mocker.patch("wifit3.chips.rtl8822cu.driver.set_channel_20mhz")

    driver._retune_channel(36)

    init.assert_called_once_with(driver.transport, cut=5, rfe_type=3)
    tune.assert_called_once_with(driver.transport, 36)


def test_transport_discovers_the_real_vendor_bulk_layout():
    class Interface(list):
        bInterfaceClass = 0xFF
        bInterfaceNumber = 0

    intf = Interface([
        SimpleNamespace(bEndpointAddress=0x84, bmAttributes=0x02),
        SimpleNamespace(bEndpointAddress=0x05, bmAttributes=0x02),
        SimpleNamespace(bEndpointAddress=0x06, bmAttributes=0x02),
        SimpleNamespace(bEndpointAddress=0x87, bmAttributes=0x02),
        SimpleNamespace(bEndpointAddress=0x08, bmAttributes=0x02),
    ])
    dev = MagicMock()
    dev.get_active_configuration.return_value = [intf]

    assert RTL8822CUTransport(dev).endpoints() == EndpointLayout(
        0, (0x84, 0x87), (0x05, 0x06, 0x08)
    )


def test_chip_info_decodes_the_hardware_probe_values():
    transport = MagicMock()
    transport.read32.side_effect = (0x0C495D35, 0xC0000013, 0x500014C9)

    info = read_chip_info(transport)

    assert info.chip_id == 0x13
    assert info.cut == 5
    assert info.rf_2t2r is True
    assert info.rom_version == 5


def test_efuse_decoder_places_enabled_words_in_the_logical_map():
    physical = bytearray(b"\xff" * 512)
    # block 2, word 0 only -> logical offsets 0x10 and 0x11.
    physical[:3] = bytes((0x2E, 0x29, 0x81))

    logical = decode_logical_map(bytes(physical))

    assert logical[0x10:0x12] == b"\x29\x81"
    assert logical[0x12] == 0xFF


def test_efuse_exposes_the_board_specific_rfe_type():
    logical = bytearray(b"\xff" * 768)
    logical[0xCA] = 0x15
    info = EfuseInfo(True, True, bytes(logical), b"\xff" * 512)
    assert info.rfe_type == 0x15


def test_bundled_rtl8822cu_firmware_has_valid_section_boundaries():
    image = load_firmware()

    assert image.version > 0
    assert len(image.dmem) >= 8
    assert len(image.imem) >= 8


def test_fw_tx_descriptor_is_48_bytes_and_has_a_checksum():
    desc = _fw_tx_desc(512)
    assert len(desc) == 48
    assert int.from_bytes(desc[:2], "little") == 512
    assert (int.from_bytes(desc[0:4], "little") >> 16) & 0xFF == 48
    assert int.from_bytes(desc[28:30], "little") != 0


def test_8822c_power_flow_uses_its_usb_specific_registers():
    transport = MagicMock()
    transport.read8.side_effect = lambda addr: 0x02 if addr == 0x0006 else 0
    card_enable_flow_8822c(transport, cut_mask=0x40)
    addresses = [call.args[0] for call in transport.write8.call_args_list]
    assert 0xFF1A in addresses
    assert 0x1018 in addresses


def test_fw_section_zlp_padding_does_not_change_iddma_length():
    transport = MagicMock()
    transport.read32.return_value = 0
    transport.read8.return_value = 0
    transport.read16.return_value = 0x8000
    dev = MagicMock()
    dev.write.side_effect = lambda ep, data, timeout: len(data)

    _upload_section(dev, transport, 0x05, 0x00200000, b"x" * 464)

    packet = dev.write.call_args.args[1]
    assert len(packet) == 48 + 465  # 464 + descriptor lands on USB ZLP boundary.
    assert transport.write32.call_args_list[-1].args[1] & 0x3FFFF == 464


def test_rx_mac_initialization_uses_8822c_monitor_rcr_and_fifo_boundary():
    transport = MagicMock()
    transport.read8.return_value = 0
    init_rx_mac(transport)

    assert (0x0608, RCR_MONITOR) in [call.args for call in transport.write32.call_args_list]
    assert (0x0204, 1996) in [call.args for call in transport.write16.call_args_list]
    assert (0x0280, 0x2005) in [call.args for call in transport.write16.call_args_list]


def test_enable_bb_rf_reverses_the_pre_init_rf_disable():
    transport = MagicMock()
    enable_bb_rf(transport)
    assert transport.write8_set.call_args_list[0].args == (0x0002, 0x03)
    assert transport.write8_set.call_args_list[1].args == (0x001F, 0x07)
    assert transport.write32_set.call_args.args == (0x00EC, 0x07000000)


def test_monitor_mode_uses_8822c_sniffer_drvinfo_and_promiscuous_filters():
    transport = MagicMock()

    enter_monitor_mode(transport)

    assert (0x0102, 0) in [call.args for call in transport.write8.call_args_list]
    assert (0x060F, 5) in [call.args for call in transport.write8.call_args_list]
    assert transport.write8_set.call_args.args == (0x060F, 0x80)
    assert transport.write32_set.call_args.args == (0x07D4, 1 << 9)
    assert (0x0608, RCR_MONITOR) in [call.args for call in transport.write32.call_args_list]
    assert {(0x06A0, 0xFFFF), (0x06A2, 0xFFFF), (0x06A4, 0xFFFF)} <= {
        call.args for call in transport.write16.call_args_list
    }


def test_rx_descriptor_yields_80211_body_without_fcs():
    # 24-byte descriptor followed by 24-byte MPDU (20 byte body + 4 byte FCS).
    descriptor = bytearray(24)
    descriptor[0:4] = (24).to_bytes(4, "little")
    mpdu = bytes(range(24))
    frames = list(iter_bulk_frames(bytes(descriptor) + mpdu))

    assert len(frames) == 1
    assert frames[0][1] == mpdu[:-4]


def test_phy_tables_select_rfe_specific_conditional_records():
    # Header chooses target 0x00ffff15; the following conditional emits one write.
    table = (
        (0xF0FFFF15, 0),
        (0x80FFFF15, 0),
        (0x40000000, 0),
        (0x1234, 0xABCDEF01),
        (0xB0000000, 0),
    )
    assert list(selected_writes(table, cut=5, rfe_type=0x15)) == [(0x1234, 0xABCDEF01)]


def test_phy_tables_have_selected_records_for_realtek_rfe_15_profile():
    assert len(list(selected_writes(load_table("bb"), cut=5, rfe_type=0x15))) > 1000
    assert len(list(selected_writes(load_table("rf_a"), cut=5, rfe_type=0x15))) > 500


def test_phy_initialization_brackets_tables_with_8822c_decoder_block_enable():
    transport = MagicMock()
    transport.read32.return_value = 0

    initialize_phy(transport, cut=5, rfe_type=3)

    decoder_block_writes = [
        call.args for call in transport.write32.call_args_list if call.args[0] == 0x1C3C
    ]
    assert decoder_block_writes[0] == (0x1C3C, 0)
    assert decoder_block_writes[-1] == (0x1C3C, 3)


def test_rf_path_b_uses_the_8822c_direct_rf_window():
    transport = MagicMock()
    _write_rf(transport, 1, 0x18, 0x123456)
    assert transport.write32.call_args.args == (0x4C60, 0x23456)


def test_2g_channel_switch_enables_cck_rx_and_20mhz_rf_filter():
    transport = MagicMock()
    transport.read32.return_value = 0

    from wifit3.chips.rtl8822cu.phy import set_channel_20mhz
    set_channel_20mhz(transport, 6)

    assert (0x454, 1 << 7) in [call.args for call in transport.write8_clr.call_args_list]
    writes = [call.args for call in transport.write32.call_args_list]
    assert (0x1A9C, 1 << 20) in writes
    assert (0x1A14, 0) in writes
    assert (0x1A80, 0) in writes
    assert (0x1C80, 0x0F000000) in writes
    assert (0x3CFC, 0x18) in writes
    assert (0x4CFC, 0x18) in writes
    assert [call.args for call in transport.write32_set.call_args_list][-2:] == [
        (0, 1 << 16), (0, 1 << 16),
    ]
    assert transport.write32_clr.call_args.args == (0, 1 << 16)
    mask_writes = [call.args for call in transport.write32.call_args_list]
    assert (0x1D70, 0x7E) in mask_writes
    assert (0x1D70, 0x7E00) in mask_writes
