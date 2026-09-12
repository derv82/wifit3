"""RTL8822BU DKMS EFUSE decode: synthetic maps, no hardware."""
from unittest.mock import MagicMock

from wifit3.chips.rtl8822bu_dkms import efuse as efuse_mod
from wifit3.chips.rtl8822bu_dkms.constants import (
    BIT_AUTOLOAD_SUS,
    EEPROM_2G_5G_PA_TYPE,
    EEPROM_2G_LNA_TYPE_GAIN_SEL_AB,
    EEPROM_5G_LNA_TYPE_GAIN_SEL_AB,
    EEPROM_CHANNEL_PLAN,
    EEPROM_COUNTRY_CODE,
    EEPROM_CUSTOM_ID,
    EEPROM_DEFAULT_CRYSTAL_CAP,
    EEPROM_DEFAULT_PID,
    EEPROM_DEFAULT_THERMAL_METER,
    EEPROM_DEFAULT_VID,
    EEPROM_MAC_ADDR,
    EEPROM_PID,
    EEPROM_RF_BOARD_OPTION,
    EEPROM_RF_BT_SETTING,
    EEPROM_RFE_OPTION,
    EEPROM_USB_MODE,
    EEPROM_VID,
    EEPROM_SIZE_8822B,
    EEPROM_THERMAL_METER,
    EEPROM_VERSION,
    EEPROM_XTAL,
    EFUSE_PA_BIAS,
    EFUSE_SIZE_8822B,
    ODM_BOARD_BT,
    ODM_BOARD_EXT_LNA,
    ODM_BOARD_EXT_LNA_5G,
    ODM_BOARD_EXT_PA,
    ODM_BOARD_EXT_PA_5G,
)


def _logical(fields: dict[int, int] | None = None, *, valid_id: bool = True) -> bytes:
    data = bytearray(b"\xff" * EEPROM_SIZE_8822B)
    data[0:2] = b"\x29\x81" if valid_id else b"\x00\x00"
    if fields:
        for off, val in fields.items():
            data[off] = val
    return bytes(data)


def _read(logical: bytes, monkeypatch, *, autoload_ok: bool = True, pa_bias=(0xF0, 0xF0)):
    phy = bytearray(b"\xff" * EFUSE_SIZE_8822B)
    phy[EFUSE_PA_BIAS] = pa_bias[0]
    phy[EFUSE_PA_BIAS + 1] = pa_bias[1]
    monkeypatch.setattr(efuse_mod, "_read_hw_efuse", lambda _t, _offset, _size: bytes(phy))
    monkeypatch.setattr(efuse_mod, "_eeprom_parser", lambda _phy: logical)
    t = MagicMock()
    t.read8.return_value = BIT_AUTOLOAD_SUS if autoload_ok else 0
    return efuse_mod.read_efuse(t)


def test_scalar_board_bt_and_identity_fields(monkeypatch):
    logical = bytearray(_logical({
        EEPROM_XTAL: 0x2A,
        EEPROM_THERMAL_METER: 0x19,
        EEPROM_RFE_OPTION: 0x03,
        EEPROM_CHANNEL_PLAN: 0x7F,
        EEPROM_COUNTRY_CODE: ord("U"),
        EEPROM_COUNTRY_CODE + 1: ord("S"),
        EEPROM_VERSION: 0x22,
        EEPROM_CUSTOM_ID: 0xAB,
        EEPROM_RF_BOARD_OPTION: 0xE5,
        EEPROM_RF_BT_SETTING: 0x41,
        EEPROM_USB_MODE: 0x80,
        EEPROM_VID: 0x57,
        EEPROM_VID + 1: 0x23,
        EEPROM_PID: 0x15,
        EEPROM_PID + 1: 0x01,
    }))
    logical[EEPROM_MAC_ADDR:EEPROM_MAC_ADDR + 6] = bytes.fromhex("001122334455")

    e = _read(bytes(logical), monkeypatch, pa_bias=(0x12, 0x34))

    assert e.crystal_cap == 0x2A
    assert e.thermal_meter == 0x19
    assert e.rfe_type == 0x03
    assert e.channel_plan == 0x7F
    assert e.country_code == b"US"
    assert e.eeprom_version == 0x22
    assert e.customer_id == 0xAB
    assert e.eeprom_id_valid is True
    assert e.usb_mode_switch is True
    assert e.eeprom_vid == 0x2357
    assert e.eeprom_pid == 0x0115
    assert e.regulatory == 0x05
    assert e.interface_sel == 0x07
    assert e.bt_coexist is False
    assert e.bt_ant_num == 1
    assert e.bt_ant_path == 1
    assert e.mac_address == "00:11:22:33:44:55"
    assert e.pa_bias == (0x12, 0x34)


def test_usb_keeps_raw_bt_fuse_but_disables_effective_coex_policy(monkeypatch):
    e = _read(_logical({EEPROM_RF_BOARD_OPTION: 0x20, EEPROM_RF_BT_SETTING: 0x00}), monkeypatch)

    assert e.interface_sel == 1
    assert e.bt_coexist_raw is True
    assert e.bt_coexist is False
    assert e.bt_ant_num == 0
    assert e.bt_ant_path == 0
    assert e.board_type & ODM_BOARD_BT == 0


def test_blank_or_invalid_board_option_disables_bt(monkeypatch):
    blank = _read(_logical({EEPROM_RF_BOARD_OPTION: 0xFF}), monkeypatch)
    invalid = _read(_logical({EEPROM_RF_BOARD_OPTION: 0x20}, valid_id=False), monkeypatch)

    assert blank.bt_coexist_raw is False
    assert blank.bt_coexist is False
    assert blank.board_type & ODM_BOARD_BT == 0
    assert invalid.bt_coexist_raw is False
    assert invalid.bt_coexist is False
    assert invalid.interface_sel == 0


def test_invalid_id_code_forces_vendor_defaults(monkeypatch):
    e = _read(_logical({
        EEPROM_XTAL: 0x2A,
        EEPROM_THERMAL_METER: 0x19,
        EEPROM_RFE_OPTION: 0x03,
        EEPROM_USB_MODE: 0x80,
    }, valid_id=False), monkeypatch)

    assert e.eeprom_id_valid is False
    assert e.autoload_fail is True
    assert e.crystal_cap == EEPROM_DEFAULT_CRYSTAL_CAP
    assert e.thermal_meter == EEPROM_DEFAULT_THERMAL_METER
    assert e.rfe_type == 0
    assert e.usb_mode_switch is False
    assert e.eeprom_vid == EEPROM_DEFAULT_VID
    assert e.eeprom_pid == EEPROM_DEFAULT_PID


def test_external_pa_lna_types_feed_the_odm_board_type(monkeypatch):
    e = _read(_logical({
        EEPROM_2G_5G_PA_TYPE: 0x33,
        EEPROM_2G_LNA_TYPE_GAIN_SEL_AB: 0xDD,
        EEPROM_5G_LNA_TYPE_GAIN_SEL_AB: 0xDD,
        EEPROM_RF_BOARD_OPTION: 0x20,
    }), monkeypatch)

    assert e.external_pa_2g is True
    assert e.external_lna_2g is True
    assert e.external_pa_5g is True
    assert e.external_lna_5g is True
    assert e.type_gpa == 5
    assert e.type_apa == 5
    assert e.type_glna == 5
    assert e.type_alna == 5
    assert e.bt_coexist_raw is True
    assert e.board_type == (ODM_BOARD_EXT_PA | ODM_BOARD_EXT_LNA | ODM_BOARD_EXT_PA_5G |
                            ODM_BOARD_EXT_LNA_5G)


def test_blank_or_invalid_scalars_take_vendor_defaults(monkeypatch):
    e = _read(_logical({
        EEPROM_XTAL: 0xFF,
        EEPROM_THERMAL_METER: 0xFF,
        EEPROM_RFE_OPTION: 0xFF,
        EEPROM_VERSION: 0x7A,
        EEPROM_CUSTOM_ID: 0x7B,
    }, valid_id=False), monkeypatch)

    assert e.crystal_cap == EEPROM_DEFAULT_CRYSTAL_CAP
    assert e.thermal_meter == EEPROM_DEFAULT_THERMAL_METER
    assert e.rfe_type == 0
    assert e.channel_plan == 0xFF
    assert e.country_code == b"\xff\xff"
    assert e.eeprom_version == 1
    assert e.customer_id == 0
    assert e.mac_address is None


def test_physical_read_sequence_keeps_halmac_shape(monkeypatch):
    monkeypatch.setattr(efuse_mod, "_read_hw_efuse", MagicMock(return_value=b"\xff" * EFUSE_SIZE_8822B))
    monkeypatch.setattr(efuse_mod, "_eeprom_parser", lambda _phy: _logical())
    t = MagicMock()
    t.read8.return_value = BIT_AUTOLOAD_SUS

    efuse_mod.read_efuse(t)

    efuse_mod._read_hw_efuse.assert_called_once_with(t, 0, EFUSE_SIZE_8822B)
