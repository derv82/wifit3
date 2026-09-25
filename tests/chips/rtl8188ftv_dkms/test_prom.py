"""rtl8188ftv_dkms M2 parses: pure byte surgery over the shadow map."""


def _map(entries=None):
    m = bytearray([0xFF] * 512)
    for addr, value in dict(entries or {}).items():
        m[addr] = value
    return bytes(m)


def test_sign4():
    from wifit3.chips.rtl8188ftv_dkms import prom
    assert prom._sign4(0x7) == 7
    assert prom._sign4(0x8) == -8
    assert prom._sign4(0xF) == -1
    assert prom._sign4(0x0) == 0


def test_check_invalid_mac():
    from wifit3.chips.rtl8188ftv_dkms import prom
    assert prom.check_invalid_mac(bytes.fromhex("44efbf1f9dfb"), True) is False
    assert prom.check_invalid_mac(b"\x00" * 6, True) is True
    assert prom.check_invalid_mac(b"\xff" * 6, True) is True
    assert prom.check_invalid_mac(bytes.fromhex("03efbf1f9dfb"), True) is True
    assert prom.check_invalid_mac(bytes.fromhex("02efbf1f9dfb"), False) is False


def test_chnl_group_2g():
    from wifit3.chips.rtl8188ftv_dkms import prom
    assert [prom.chnl_group_2g(ch) for ch in (1, 2)] == [0, 0]
    assert [prom.chnl_group_2g(ch) for ch in (3, 5)] == [1, 1]
    assert [prom.chnl_group_2g(ch) for ch in (6, 8)] == [2, 2]
    assert [prom.chnl_group_2g(ch) for ch in (9, 11)] == [3, 3]
    assert [prom.chnl_group_2g(ch) for ch in (12, 14)] == [4, 4]


def test_power_value_autoload_fail_defaults():
    from wifit3.chips.rtl8188ftv_dkms import prom
    out = prom.read_power_value(bytes(512), True)
    assert out.cck_base[0] == [0x2D] * 6
    assert out.bw20_diff[0] == [0x02, 0xFE, 0xFE, 0xFE]
    assert out.ofdm_diff[0] == [0x04, 0xFE, 0xFE, 0xFE]
    assert out.cck_diff[0] == [0xFE] * 4


def test_power_value_ff_forces_fail():
    from wifit3.chips.rtl8188ftv_dkms import prom
    m = _map({0x11: 0xFF})
    out = prom.read_power_value(m, False)
    assert out.cck_base[0] == [0x2D] * 6


def test_thermal_crystal_customer_defaults_on_fail():
    from wifit3.chips.rtl8188ftv_dkms import prom
    params = prom.EfuseParams()
    prom.parse_thermal(bytes(512), True, params)
    assert params.thermal == 0x18
    prom.parse_eeprom_ver(bytes(512), True, params)
    assert params.version == 1
    params2 = prom.EfuseParams()
    prom.parse_eeprom_ver(_map({0xC4: 0x07}), False, params2)
    assert params2.version == 0x07


def test_power_saving_paths():
    from wifit3.chips.rtl8188ftv_dkms import prom
    params = prom.EfuseParams()
    prom.parse_power_saving(bytes(512), True, params)
    assert params.hw_powerdown is False and params.remote_wakeup is False
    params = prom.EfuseParams()
    prom.parse_power_saving(_map({0xC2: 0x10, 0xD4: 0x02}), False, params)
    assert params.hw_powerdown == 0x10 and params.remote_wakeup is True
    params = prom.EfuseParams()
    prom.parse_power_saving(bytes(512), False, params, hwpdn_mode=1)
    assert params.hw_powerdown == 1
