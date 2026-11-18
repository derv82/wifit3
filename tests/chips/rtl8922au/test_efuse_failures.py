import logging

import pytest

from wifit3.chips.rtl8922au import mac
from wifit3.chips.rtl8922au.constants import (
    B_BE_AUTOLOAD_SUS, CHIP_CAV, PHYSICAL_EFUSE_SIZE, R_BE_SYS_WL_EFUSE_CTRL,
)

EMPTY_EFUSE_MAP = b"\xff" * PHYSICAL_EFUSE_SIZE


def _transport(mocker, autoload: int, mac_word: int):
    t = mocker.Mock(gain_offset=[[0] * 5, [0] * 5])
    t.read16.side_effect = lambda addr: autoload if addr == R_BE_SYS_WL_EFUSE_CTRL else mac_word
    return t


def test_parse_efuse_map_warns_when_autoload_is_not_set(mocker, caplog):
    mocker.patch.object(mac, "dump_physical_efuse_map", return_value=EMPTY_EFUSE_MAP)
    t = _transport(mocker, 0, 0x1234)
    mac.parse_efuse_map(t, CHIP_CAV)
    assert "failed to check efuse autoload" in caplog.text


def test_parse_efuse_map_warns_and_fails_when_the_dump_fails(mocker, caplog):
    mocker.patch.object(mac, "dump_physical_efuse_map", side_effect=TimeoutError)
    t = _transport(mocker, B_BE_AUTOLOAD_SUS, 0x1234)
    with pytest.raises(TimeoutError):
        mac.parse_efuse_map(t, CHIP_CAV)
    assert "failed to dump efuse physical map" in caplog.text


def test_parse_efuse_map_fails_when_the_map_ends_inside_a_block(mocker, caplog):
    mocker.patch.object(mac, "dump_physical_efuse_map", return_value=bytes(8))
    t = _transport(mocker, B_BE_AUTOLOAD_SUS, 0x1234)
    with pytest.raises(RuntimeError):
        mac.parse_efuse_map(t, CHIP_CAV)
    assert "failed to dump efuse logical block 3" in caplog.text
    assert "failed to parse efuse logic block 2" in caplog.text


def test_parse_efuse_map_warns_and_fails_when_the_rf_block_parse_fails(mocker, caplog):
    mocker.patch.object(mac, "dump_physical_efuse_map", return_value=EMPTY_EFUSE_MAP)
    mocker.patch.object(mac, "_parse_logical_efuse_block", side_effect=[b"", RuntimeError])
    t = _transport(mocker, B_BE_AUTOLOAD_SUS, 0x1234)
    with pytest.raises(RuntimeError):
        mac.parse_efuse_map(t, CHIP_CAV)
    assert "failed to dump efuse logical block 1" in caplog.text
    assert "failed to parse efuse logic block 1" in caplog.text


def test_parse_efuse_map_uses_a_random_mac_when_the_efuse_mac_is_zero(mocker, caplog):
    caplog.set_level(logging.INFO)
    mocker.patch.object(mac, "dump_physical_efuse_map", return_value=EMPTY_EFUSE_MAP)
    t = _transport(mocker, B_BE_AUTOLOAD_SUS, 0)
    addr = mac.parse_efuse_map(t, CHIP_CAV)["mac_addr"]
    assert any(addr)
    assert addr[0] & 0x03 == 0x02
    assert "efuse mac address is zero, using random mac" in caplog.text


def test_parse_phycap_map_warns_and_fails_when_the_dump_fails(mocker, caplog):
    mocker.patch.object(mac, "dump_physical_efuse_map", side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        mac.parse_phycap_map(mocker.Mock(), CHIP_CAV)
    assert "failed to dump phycap map" in caplog.text
