from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time
from unittest.mock import Mock

import pytest

from wifit3.chips.rtl8822cu import dack_cache, driver
from wifit3.chips.rtl8822cu.cal import DackState
from wifit3.chips.rtl8822cu.chipid import ChipInfo

from tests.chips.rtl8822cu.test_driver import _driver, _efuse, _run_bringup


@pytest.fixture(autouse=True)
def clear_cache(monkeypatch):
    monkeypatch.setenv("WIFIT3_8822CU_DACK", "1")
    with dack_cache._lock:
        dack_cache._entries.clear()
    yield
    with dack_cache._lock:
        dack_cache._entries.clear()


def factory_efuse(mac=b"\x00\x12\x34\x56\x78\x9a"):
    efuse = _efuse(0x33)
    logical = bytearray(efuse.logical_map)
    logical[0x157:0x15d] = mac
    return replace(efuse, logical_map=bytes(logical))


def chip():
    return ChipInfo(driver.CHIP_ID_RTL8822CU, 5, True, 0, 0, 0, 0, 5, 0)


def calibrated():
    state = DackState(dack_en=True)
    state.valid = True
    state.msbk_d[0][0][0] = 17
    return state


def test_cache_is_off_by_default(monkeypatch):
    monkeypatch.delenv("WIFIT3_8822CU_DACK")
    key = dack_cache.identity(factory_efuse(), chip())
    dack_cache.put(key, calibrated())
    assert dack_cache.get(key) is None


def test_identity_requires_valid_factory_efuse_and_global_unicast_mac():
    efuse = factory_efuse()
    assert dack_cache.identity(efuse, chip()) is not None
    for mac in (bytes(6), b"\xff" * 6, b"\x01\x12\x34\x56\x78\x9a",
                b"\x02\x12\x34\x56\x78\x9a"):
        assert dack_cache.identity(factory_efuse(mac), chip()) is None
    for bad in (replace(efuse, autoload_ok=False), replace(efuse, map_valid=False),
                replace(efuse, logical_map=bytes(768)), replace(efuse, physical_map=b"")):
        assert dack_cache.identity(bad, chip()) is None
    assert dack_cache.identity(efuse, replace(chip(), chip_id=0)) is None
    assert dack_cache.identity(efuse, chip()) != dack_cache.identity(efuse, replace(chip(), cut=6))
    assert dack_cache.identity(efuse, chip()) != dack_cache.identity(factory_efuse(b"\x00\x12\x34\x56\x78\x9b"), chip())


def test_process_lifetime_validity_and_copy_isolation(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    key = dack_cache.identity(factory_efuse(), chip())
    state = calibrated()
    dack_cache.put(key, DackState(dack_en=True))
    assert dack_cache.get(key) is None
    dack_cache.put(key, state)
    state.msbk_d[0][0][0] = 99
    result = dack_cache.get(key)
    assert result.msbk_d[0][0][0] == 17
    result.msbk_d[0][0][0] = 33
    assert dack_cache.get(key).msbk_d[0][0][0] == 17
    now[0] += 365 * 24 * 60 * 60
    assert dack_cache.get(key).msbk_d[0][0][0] == 17


def test_capacity_evicts_oldest_insertion_not_most_recently_used():
    keys = [(number,) for number in range(33)]
    for key in keys[:32]:
        dack_cache.put(key, calibrated())
    assert dack_cache.get(keys[0]) is not None
    dack_cache.put(keys[1], calibrated())
    assert len(dack_cache._entries) == 32
    dack_cache.put(keys[32], calibrated())
    assert len(dack_cache._entries) == 32
    assert dack_cache.get(keys[0]) is None
    assert all(dack_cache.get(key) is not None for key in keys[1:])


def test_concurrent_access_returns_independent_copies():
    key = dack_cache.identity(factory_efuse(), chip())
    dack_cache.put(key, calibrated())

    def read_and_modify(value):
        state = dack_cache.get(key)
        state.msbk_d[0][0][0] = value

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(read_and_modify, range(100)))
    assert dack_cache.get(key).msbk_d[0][0][0] == 17


def test_bringup_uses_cached_state_in_new_instance(monkeypatch):
    efuse = factory_efuse()
    key = dack_cache.identity(efuse, chip())
    dack_cache.put(key, calibrated())
    instance, _ = _run_bringup(monkeypatch, efuse)
    assert instance._dack_restored
    assert instance.dack.msbk_d[0][0][0] == 17
    assert instance.dack is not dack_cache.get(key)


def test_restore_exception_evicts_and_aborts(monkeypatch):
    efuse = factory_efuse()
    key = dack_cache.identity(efuse, chip())
    dack_cache.put(key, calibrated())
    instance, _ = _run_bringup(monkeypatch, efuse)
    monkeypatch.setattr(driver, "odm_dm_init", Mock(side_effect=RuntimeError("restore failed")))
    with pytest.raises(RuntimeError, match="restore failed"):
        instance._bringup(instance.transport)
    assert dack_cache.get(key) is None


async def test_publish_only_after_successful_connect(monkeypatch):
    instance = _driver()
    instance.transport.endpoints = Mock(return_value=Mock(bulk_in=[1], bulk_out=[2, 3, 4], bulk_out_max_packet_size=512))
    instance._claim = Mock()
    instance._bringup = Mock()
    instance._monitor_entry = Mock(side_effect=RuntimeError("failed"))
    instance.close = MockAsync()
    instance._dack_cache_key = dack_cache.identity(factory_efuse(), chip())
    instance.dack = calibrated()
    with pytest.raises(Exception, match="failed"):
        await instance.connect()
    assert dack_cache.get(instance._dack_cache_key) is None


async def test_successful_connect_publishes_calibration(monkeypatch):
    instance = _driver()
    instance.transport.endpoints = Mock(return_value=Mock(bulk_in=[1], bulk_out=[2, 3, 4], bulk_out_max_packet_size=512))
    instance._claim = Mock()
    instance._bringup = Mock()
    instance._monitor_entry = Mock()
    instance._dack_cache_key = dack_cache.identity(factory_efuse(), chip())
    instance.dack = calibrated()
    monkeypatch.setattr(driver, "RxReaderThread", Mock())
    monkeypatch.setattr(driver.WatchdogState, "from_phydm", Mock())
    monkeypatch.setattr(driver.asyncio, "create_task", lambda coro, **kwargs: coro.close())
    assert await instance.connect()
    assert dack_cache.get(instance._dack_cache_key).msbk_d[0][0][0] == 17


class MockAsync:
    async def __call__(self):
        pass
