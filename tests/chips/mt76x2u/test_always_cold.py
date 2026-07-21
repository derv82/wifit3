"""connect() always cold-boots: it never reuses already-running firmware.

MT_MCU_COM_REG0 only reports that *some* firmware is up, not *whose* (a prior
wifit3 session, a stale MCU, or the kernel mt76x2u driver's own firmware). So
connect() power-cycles the WLAN block whenever anything is running and always
re-uploads our firmware. There is no warm-skip path and no warm fallback.

These drive the real connect() with the heavy helpers mocked, stopping at the
first MCU command (mcu_load_cr) so the post-init machinery stays out of scope.
"""
from unittest.mock import MagicMock

import pytest

import wifit3.chips.mt76x2u.driver as drv
from wifit3.chips.driver import DeviceID
from wifit3.chips.mt76x2u.constants import MT_ASIC_VERSION, MT_MCU_COM_REG0
from wifit3.chips.mt76x2u.driver import MT76x2UDriver
from wifit3.errors import BringUpError

WARM_COM_REG = 0x801140FB   # bits 0/1 set: firmware running
COLD_COM_REG = 0x00000000   # firmware not running


class FakeTransport:
    def __init__(self, com_reg: int):
        self._com_reg = com_reg
        self.writes: list[tuple[int, int]] = []

    def read32(self, addr: int) -> int:
        if addr == MT_ASIC_VERSION:
            return 0x76120044
        if addr == MT_MCU_COM_REG0:
            return self._com_reg
        return 0

    def write32(self, addr: int, value: int) -> None:
        self.writes.append((addr, value & 0xFFFFFFFF))

    def assert_expected_endpoints(self) -> None:
        pass


def _install(monkeypatch, com_reg: int, *, cold_ok: bool, load_cr_ok: bool):
    """Build a driver whose bring-up is fully mocked up to mcu_load_cr, and
    return (driver, counters). `counters` tracks force_power_cycle / cold-init."""
    d = MT76x2UDriver.from_usb_device(MagicMock(), DeviceID(0x0e8d, 0x7612, "test"))
    d.transport = FakeTransport(com_reg)

    counters = {"power_cycle": 0, "cold_init": 0, "mac_tables": 0}

    async def fake_power_cycle(_transport):
        counters["power_cycle"] += 1

    async def fake_cold_init(progress_cb=None):
        counters["cold_init"] += 1
        return cold_ok

    async def fake_mac_tables(mac_bytes, progress_cb=None):
        counters["mac_tables"] += 1
        return True

    async def fake_load_cr(*a, **kw):
        return load_cr_ok

    monkeypatch.setattr(d, "_claim_interface", lambda: None)
    monkeypatch.setattr(d, "_cold_init_chip", fake_cold_init)
    monkeypatch.setattr(d, "_init_mac_tables", fake_mac_tables)
    monkeypatch.setattr(drv, "force_power_cycle", fake_power_cycle)
    monkeypatch.setattr(drv, "mcu_load_cr", fake_load_cr)

    # EEPROM reads (chip-side; irrelevant to the cold/warm decision).
    monkeypatch.setattr(drv, "read_chip_id", lambda t: 0x7612)
    monkeypatch.setattr(drv, "read_mac_address", lambda t: "00:11:22:33:44:55")
    monkeypatch.setattr(drv, "read_nic_conf_0",
                        lambda t: {"raw": 0, "rx_path": 2, "tx_path": 2,
                                   "pa_int_2g": True, "pa_int_5g": False})
    monkeypatch.setattr(drv, "read_nic_conf_1",
                        lambda t: {"raw": 0, "lna_ext_2g": False,
                                   "lna_ext_5g": False, "tx_alc_en": False})
    monkeypatch.setattr(drv, "read_block", lambda t, a, n: b"\xff\xff\xff\xff")
    monkeypatch.setattr(drv, "read_rx_high_gain_2g", lambda t: (0, 0))
    monkeypatch.setattr(drv, "eeprom_tssi_enabled", lambda t: False)
    return d, counters


@pytest.mark.asyncio
async def test_warm_chip_power_cycles_then_cold_boots(monkeypatch):
    """Firmware already running → power-cycle, then always upload our own."""
    d, c = _install(monkeypatch, WARM_COM_REG, cold_ok=True, load_cr_ok=False)

    with pytest.raises(BringUpError) as exc:
        await d.connect()

    # Reached mcu_load_cr via the cold path and raised there (no warm fallback).
    assert exc.value.stage == "mcu-load-cr"
    assert c["power_cycle"] == 1     # warm → one power cycle
    assert c["cold_init"] == 1       # always upload our firmware
    assert d.is_warm is False        # never reports warm


@pytest.mark.asyncio
async def test_cold_chip_skips_power_cycle(monkeypatch):
    """Firmware not running → no power cycle, still cold-boots."""
    d, c = _install(monkeypatch, COLD_COM_REG, cold_ok=True, load_cr_ok=False)

    with pytest.raises(BringUpError) as exc:
        await d.connect()

    assert exc.value.stage == "mcu-load-cr"
    assert c["power_cycle"] == 0     # cold → nothing to power-cycle
    assert c["cold_init"] == 1
    assert d.is_warm is False


@pytest.mark.asyncio
async def test_mcu_load_cr_failure_does_not_retry(monkeypatch):
    """A warm start that fails mcu_load_cr raises once — no second cold init."""
    d, c = _install(monkeypatch, WARM_COM_REG, cold_ok=True, load_cr_ok=False)

    with pytest.raises(BringUpError):
        await d.connect()

    # Old warm fallback re-ran cold init + mac tables. Always-cold does not.
    assert c["cold_init"] == 1
    assert c["mac_tables"] == 1


@pytest.mark.asyncio
async def test_cold_init_failure_raises_cold_init(monkeypatch):
    """If our firmware upload fails, connect() raises at the cold-init stage."""
    d, c = _install(monkeypatch, WARM_COM_REG, cold_ok=False, load_cr_ok=True)

    with pytest.raises(BringUpError) as exc:
        await d.connect()

    assert exc.value.stage == "cold-init"
    assert c["power_cycle"] == 1     # power-cycled before the (failed) upload
    assert c["cold_init"] == 1
