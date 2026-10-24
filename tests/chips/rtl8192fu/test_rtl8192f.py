"""rtl8192fu 8192f.c port: EFUSE parse, TX power, LED, kfree."""
import pytest

from wifit3.chips.rtl8192fu import constants as C
from wifit3.chips.rtl8192fu.core import LED_OFF, u32_get_bits
from wifit3.chips.rtl8192fu.rtl8192f import (
    rtl8192f_set_tx_power,
    rtl8192fu_config_kfree,
    rtl8192fu_led_brightness_set,
    rtl8192fu_parse_efuse,
)

from .fake import make_priv


def _efuse() -> bytearray:
    raw = bytearray(b"\xff" * C.EFUSE_MAP_LEN)
    raw[0:2] = (0x8129).to_bytes(2, "little")
    for base, cck, ht40 in ((0x10, 0x20, 0x28), (0x3A, 0x21, 0x29)):
        raw[base:base + 6] = bytes([cck] * 6)
        raw[base + 6:base + 11] = bytes([ht40] * 5)
        raw[base + 11] = 0xE0                          # ht20_ofdm_1s_diff.b = -2
        raw[base + 12:base + 14] = bytes([0x3F, 0x0D])  # pwr_diff[0]: ht20 -1, ht40 3, ofdm -3
    raw[0xB9] = 0xE5
    raw[0xCA] = 0x25
    raw[0x107:0x10D] = bytes.fromhex("020000000001")
    return raw


def test_parse_efuse_fields():
    priv, _ = make_priv()
    priv.efuse_wifi = _efuse()
    rtl8192fu_parse_efuse(priv)

    assert priv.mac_addr == bytes.fromhex("020000000001")
    assert priv.default_crystal_cap == 0x25
    assert priv.rfe_type == 5
    assert priv.cck_tx_power_index_B == [0x21] * 6
    assert priv.ht40_1s_tx_power_index_A == [0x28] * 5
    assert priv.ht20_tx_power_diff[0] == (-2, -2)
    assert priv.ht20_tx_power_diff[1] == (-1, -1)
    assert priv.ht40_tx_power_diff[1] == (3, 3)
    assert priv.ofdm_tx_power_diff[0] == (0, 0)
    assert priv.ofdm_tx_power_diff[1] == (-3, -3)


def test_parse_efuse_rejects_bad_rtl_id():
    priv, _ = make_priv()
    priv.efuse_wifi = bytearray(C.EFUSE_MAP_LEN)
    with pytest.raises(ValueError):
        rtl8192fu_parse_efuse(priv)


def test_set_tx_power_path_b_ofdm_uses_rf_b_diff():
    priv, transport = make_priv()
    priv.efuse_wifi = _efuse()
    rtl8192fu_parse_efuse(priv)
    priv.tx_paths = 2
    rtl8192f_set_tx_power(priv, 6, False)

    writes = {address: value for address, _, value in transport.writes()}
    assert writes[C.REG_TX_AGC_A_RATE18_06] == 0x28282828   # ofdm_tx_power_diff[RF_A].a is 0
    assert writes[C.REG_TX_AGC_B_RATE18_06] == 0x26262626   # 0x29 + ofdm_tx_power_diff[RF_B].b (-3)


def test_led_hw_control_matches_capture():
    priv, transport = make_priv({C.REG_LEDCFG0: 0x00C28282})
    rtl8192fu_led_brightness_set(priv, C.RTL8XXXU_HW_LED_CONTROL)
    assert transport.writes()[-1] == (C.REG_LEDCFG0, 4, 0x00E20303)


def test_led_off_is_software_controlled_off():
    priv, transport = make_priv({C.REG_LEDCFG0: 0x00C28282})
    rtl8192fu_led_brightness_set(priv, LED_OFF)
    ledcfg = transport.writes()[-1][2]
    assert u32_get_bits(ledcfg, C.LEDCFG0_LED0CM) == C.LED_MODE_SW_CTRL
    assert u32_get_bits(ledcfg, C.LEDCFG0_LED1CM) == C.LED_MODE_SW_CTRL
    assert u32_get_bits(ledcfg, C.LEDCFG0_LED0SV) == C.LED_SW_OFF


def test_config_kfree_stops_when_efuse_0x1ee_unprogrammed():
    priv, transport = make_priv({C.REG_EFUSE_CTRL: C.BIT(31) | 0xFF})
    priv.rf_paths = 2
    rtl8192fu_config_kfree(priv, 6)
    efuse_addresses = [value for address, width, value in transport.writes() if address == C.REG_EFUSE_CTRL + 1]
    assert efuse_addresses == [0xEE, 0xEC, 0xEA]
    assert not [w for w in transport.writes() if w[0] in (C.REG_FPGA0_XA_LSSI_PARM, C.REG_FPGA0_XB_LSSI_PARM)]
