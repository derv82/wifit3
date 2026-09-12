"""Hardware-free regression for the EFUSE-derived branch selectors.

The reference AWUS036ACS burns blank amplifier bytes (0x00 -> all flags off), so the
generalized decode must reproduce the reference (board_type 0 / ext_lna_2g False) while
lighting the right ODM board bits for an external-PA/LNA card. Also pins that board_type
actually changes which phy_cond rows a card walks (the whole point of threading it).
"""
from types import SimpleNamespace

from wifit3.chips.rtl8821au_dkms import efuse
from wifit3.chips.rtl8821au_dkms.phy_cond import JaguarParams, apply_table


def _map(pa=0x00, lna2g=0x00, lna5g=0x00, valid_id=True):
    """512 B logical efuse map with key parser bytes set (rest unburned = 0xFF)."""
    m = bytearray(b"\xFF" * 512)
    if valid_id:
        m[0:2] = efuse.C.RTL_EEPROM_ID.to_bytes(2, "little")
    m[efuse.C.EEPROM_PA_TYPE_8821AU] = pa
    m[efuse.C.EEPROM_LNA_TYPE_2G_8821AU] = lna2g
    m[efuse.C.EEPROM_LNA_TYPE_5G_8821AU] = lna5g
    return bytes(m)


def test_reference_all_internal():
    # AWUS036ACS reads 0x00 across the amplifier bytes -> every external flag off.
    assert efuse._ext_amplifier_flags(_map(), autoload_fail=False) == (0, 0, 0, 0, False, False, False, False)
    assert efuse._parse_board_type((0, 0, 0, 0, False, False, False, False)) == 0


def test_autoload_fail_forces_internal():
    # An autoload-fail efuse takes the registry-AUTO else path -> all flags 0.
    assert efuse._ext_amplifier_flags(_map(pa=0xFF, lna2g=0xFF, lna5g=0xFF),
                                      autoload_fail=True) == (0, 0, 0, 0, False, False, False, False)


def test_blank_bytes_default_zero():
    # 0xFF (unburned) bytes decode to 0, same as internal.
    assert efuse._ext_amplifier_flags(_map(pa=0xFF, lna2g=0xFF, lna5g=0xFF),
                                      autoload_fail=False) == (0, 0, 0, 0, False, False, False, False)


def test_ext_lna_2g_single_bit():
    # ExternalLNA_2G keys on LNAType_2G[3] only.
    assert efuse._ext_amplifier_flags(_map(lna2g=0x08), autoload_fail=False)[5] is True
    assert efuse._ext_amplifier_flags(_map(lna2g=0x04), autoload_fail=False)[5] is False


def test_ext_flags_full_decode():
    # PA[4]=ext_pa_2g, PA[0]=ext_pa_5g (both from 0xBC); LNA2G[3]; LNA5G[3].
    flags = efuse._ext_amplifier_flags(_map(pa=0x11, lna2g=0x08, lna5g=0x08), autoload_fail=False)
    assert flags == (0x11, 0x08, 0x11, 0x08, True, True, True, True)


def test_bt_coexist_policy_uses_multi_func_ctrl_for_8821():
    m = bytearray(_map())
    m[efuse.C.EEPROM_RF_BOARD_OPTION_8821AU] = 0x00
    m[efuse.C.EEPROM_RF_BT_SETTING_8821] = 0x01

    assert efuse._parse_bt_coexist(bytes(m), efuse.C.BIT_BT_FUNC_EN, autoload_fail=False) == (True, 1)
    assert efuse._parse_bt_coexist(bytes(m), 0, autoload_fail=False) == (False, 1)
    assert efuse._parse_bt_coexist(bytes(m), efuse.C.BIT_BT_FUNC_EN, autoload_fail=True) == (False, 1)


def test_idcode_controls_autoload_defaults():
    m = bytearray(_map(valid_id=True))
    m[efuse.C.EEPROM_XTAL] = 0x2A
    m[efuse.C.EEPROM_VERSION_8821] = 0x03
    m[efuse.C.EEPROM_THERMAL_METER_8821] = 0x1A

    assert efuse._eeprom_id_valid(bytes(m)) is True
    assert efuse._parse_crystal_cap(bytes(m), autoload_fail=False) == 0x2A
    assert efuse._parse_eeprom_version(bytes(m), autoload_fail=False) == 0x03
    assert efuse._parse_thermal_meter(bytes(m), autoload_fail=False) == (0x1A, False)
    assert efuse._eeprom_id_valid(_map(valid_id=False)) is False
    assert efuse._parse_crystal_cap(bytes(m), autoload_fail=True) == efuse.C.EEPROM_DEFAULT_CRYSTAL_CAP
    assert efuse._parse_eeprom_version(bytes(m), autoload_fail=True) == efuse.C.EEPROM_DEFAULT_VERSION
    assert efuse._parse_thermal_meter(bytes(m), autoload_fail=True) == (0xFF, True)


def test_ids_and_customer_overrides():
    m = bytearray(_map())
    m[efuse.C.EEPROM_VID_8821AU:efuse.C.EEPROM_VID_8821AU + 2] = (0x2001).to_bytes(2, "little")
    m[efuse.C.EEPROM_PID_8821AU:efuse.C.EEPROM_PID_8821AU + 2] = (0x3314).to_bytes(2, "little")
    m[efuse.C.EEPROM_CUSTOM_ID_8812] = efuse.C.EEPROM_CID_DEFAULT

    assert efuse._parse_ids(bytes(m), autoload_fail=False) == (
        0x2001, 0x3314, efuse.C.EEPROM_CID_DEFAULT, efuse.C.EEPROM_DEFAULT_SUBCUSTOMER_ID,
        efuse.C.RT_CID_DLINK)
    assert efuse._parse_ids(bytes(m), autoload_fail=True) == (
        efuse.C.EEPROM_DEFAULT_VID, efuse.C.EEPROM_DEFAULT_PID,
        efuse.C.EEPROM_DEFAULT_CUSTOMER_ID, efuse.C.EEPROM_DEFAULT_SUBCUSTOMER_ID,
        efuse.C.RT_CID_DEFAULT)


def test_board_option_country_usb_and_mac_parsers():
    m = bytearray(_map())
    m[efuse.C.EEPROM_RF_BOARD_OPTION_8821AU] = 0xA5
    m[efuse.C.EEPROM_COUNTRY_CODE_8812:efuse.C.EEPROM_COUNTRY_CODE_8812 + 2] = b"US"
    m[efuse.C.EEPROM_USB_OPTIONAL_FUNCTION0_8811AU] = efuse.C.BIT1
    m[efuse.C.EEPROM_USB_MODE_8812] = efuse.C.BIT1
    m[efuse.C.EEPROM_MAC_ADDR_8821AU:efuse.C.EEPROM_MAC_ADDR_8821AU + 6] = b"\x02\x11\x22\x33\x44\x55"

    assert efuse._parse_regulatory(bytes(m), autoload_fail=False) == 0x05
    assert efuse._parse_interface_sel(bytes(m), autoload_fail=False) == 0x05
    assert efuse._parse_country_code(bytes(m)) == "US"
    assert efuse._parse_remote_wakeup(bytes(m), autoload_fail=False) is True
    assert efuse._parse_usb_mode_switch(bytes(m), autoload_fail=False) is True
    assert efuse._parse_mac_address(bytes(m), autoload_fail=False) == "02:11:22:33:44:55"
    assert efuse._parse_mac_address(bytes(m), autoload_fail=True) is None


def test_hidden_usb_type_decode():
    hidden = {efuse.C.EFUSE_HIDDEN_USB_TYPE_ANTENNA_0: 0x04,
              efuse.C.EFUSE_HIDDEN_USB_TYPE_WMODE_0: 0x08}

    assert efuse._parse_usb_type_hidden(hidden, _map()) == (2, 2, True)



def test_board_type_bits():
    bt = efuse._parse_board_type((0, 0, 0, 0, True, True, True, True), bt_coexist=True)
    assert bt == (efuse.ODM_BOARD_BT | efuse.ODM_BOARD_EXT_PA_2G | efuse.ODM_BOARD_EXT_LNA_2G
                  | efuse.ODM_BOARD_EXT_PA_5G | efuse.ODM_BOARD_EXT_LNA_5G)
    assert efuse._parse_board_type((0, 0, 0, 0, False, True, False, False)) == efuse.ODM_BOARD_EXT_LNA_2G


class _UnusedTransport:
    pass


def _read_logical_map_from(raw, monkeypatch):
    data = dict(enumerate(raw))
    monkeypatch.setattr(efuse, "_efuse_one_byte_read", lambda t, addr: data.get(addr, 0xFF))
    return efuse._read_logical_map(_UnusedTransport())


def test_logical_map_continues_after_disabled_extended_header(monkeypatch):
    m = _read_logical_map_from([0x0F, 0x0F, 0x2E, 0x34, 0x12, 0xFF], monkeypatch)

    assert m[0x10:0x12] == b"\x34\x12"


def test_logical_map_invalid_extended_offset_skips_enabled_words(monkeypatch):
    m = _read_logical_map_from([0x0F, 0x8E, 0xAA, 0xBB, 0x1E, 0x78, 0x56, 0xFF], monkeypatch)

    assert m[0x08:0x0A] == b"\x78\x56"


def test_build_jaguar_params_threads_board_type():
    jp = efuse.build_jaguar_params(SimpleNamespace(board_type=0x98))
    assert jp.board_type == 0x98
    assert jp.cut_version == 0          # 8821 tables carry no cut-gated rows
    assert (jp.support_interface, jp.support_platform) == (0x02, 0x04)   # USB / CE defaults
    assert (jp.type_glna, jp.type_gpa, jp.type_alna, jp.type_apa) == (0, 0, 0, 0)


# A synthetic phy_cond block gated exactly like the real 8821a AGC row 0x8000020c:
# taken only when the USB interface AND the ALNA|APA (5 GHz ext PA+LNA) board bits are set.
_BOARD_GATED = [
    0x8000020C, 0x00000000,   # IF: interface-USB + ALNA(bit2) + APA(bit3)
    0x40000000, 0x00000000,   # paired negative-condition row (type_* all 0)
    0x00000100, 0x0000AAAA,   # data row — only emitted when the IF matches
    0xB0000000, 0x00000000,   # ENDIF
]


def _collect(table, params):
    out = []
    apply_table(table, lambda a, v: out.append((a, v)), params)
    return out


def test_board_type_gates_walker_row():
    # Reference (board_type 0): the ALNA|APA-gated row is skipped.
    assert _collect(_BOARD_GATED, JaguarParams(board_type=0)) == []
    # Ext 5 GHz PA+LNA card (APA|ALNA): the same row is now walked.
    variant = efuse.ODM_BOARD_EXT_PA_5G | efuse.ODM_BOARD_EXT_LNA_5G
    assert _collect(_BOARD_GATED, JaguarParams(board_type=variant)) == [(0x100, 0xAAAA)]
