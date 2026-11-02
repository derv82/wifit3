"""RTL8187Driver.connect() / close() glue, with every register-touching step mocked."""
from __future__ import annotations

from unittest.mock import MagicMock

import wifit3.chips.rtl8187.driver as drv
from wifit3.chips.rtl8187.constants import HwRev
from wifit3.chips.rtl8187.mac import ChipVariant
from wifit3.chips.rtl8187.probe import ProbeResult
from wifit3.chips.rtl8187.rtl8225 import RfSetup, RfVariant, TxPower

_POWER = TxPower(hw_value=tuple([0x55] * 14), base=0x36)
_SETUP = RfSetup(asic_rev=1, variant=RfVariant.RTL8225Z2, hw_rev=None)


def _probe_result() -> ProbeResult:
    return ProbeResult(
        mac=bytes.fromhex("020000000001"),
        chip=ChipVariant(hwver_raw=0, name="RTL8187vB", is_rtl8187b=False, hw_rev=None),
        setup=_SETUP,
        power=_POWER,
        rfkill_mask=0x2,
        rfkill_off=True,
    )


def _mock_bring_up(monkeypatch, calls: list[tuple]) -> drv.RTL8187Driver:
    d = drv.RTL8187Driver(MagicMock())
    monkeypatch.setattr(d, "_claim", lambda: calls.append(("claim",)))
    monkeypatch.setattr(drv, "probe", lambda t, product_id, is_rtl8187b: (calls.append(("probe",)), _probe_result())[1])
    monkeypatch.setattr(drv, "bulk_out_max_packet_size", lambda dev, ep: 64)
    monkeypatch.setattr(drv, "build_rf_init", lambda t, setup, power: lambda _t: None)
    monkeypatch.setattr(drv, "cold_bring_up",
                        lambda t, rf_init, is_rtl8187b: (calls.append(("cold_bring_up",)), 0x909CFC0B)[1])
    monkeypatch.setattr(drv, "_config_channel",
                        lambda t, setup, ch, power:
                        calls.append(("config_channel", setup, ch, power)))
    monkeypatch.setattr(drv, "RxReaderThread", MagicMock())
    return d


async def test_connect_tunes_channel_1_after_monitor_entry(monkeypatch):
    calls: list[tuple] = []
    d = _mock_bring_up(monkeypatch, calls)
    assert d.current_channel is None

    assert await d.connect() is True

    assert calls == [
        ("claim",),
        ("probe",),
        ("cold_bring_up",),
        ("config_channel", _SETUP, 1, _POWER),
    ]
    assert d.current_channel == 1
    assert d._bulk_out_max_packet_sizes == {0x02: 64}
    assert d._bulk_in_ep == 0x81                       # usb_rcvbulkpipe(.., 1), dev.c:435


async def test_close_runs_stop_before_rx_teardown_and_release(monkeypatch):
    calls: list[tuple] = []
    d = _mock_bring_up(monkeypatch, calls)
    await d.connect()
    calls.clear()

    monkeypatch.setattr(drv, "stop", lambda t, rf_stop, is_rtl8187b: calls.append(("stop",)))

    async def rx_stop() -> None:
        calls.append(("rx_stop",))

    d._rx_reader = MagicMock(stop=rx_stop)
    monkeypatch.setattr(d, "_release", lambda: calls.append(("release",)))

    await d.close()
    assert calls == [("stop",), ("rx_stop",), ("release",)]


async def test_close_before_connect_skips_stop(monkeypatch):
    d = drv.RTL8187Driver(MagicMock())
    monkeypatch.setattr(drv, "stop", lambda t, rf_stop, is_rtl8187b: (_ for _ in ()).throw(AssertionError))
    await d.close()


async def test_connect_brings_up_8187b_with_the_untested_warning(monkeypatch, caplog):
    import dataclasses
    import logging

    calls: list[tuple] = []
    d = _mock_bring_up(monkeypatch, calls)
    b_setup = RfSetup(asic_rev=1, variant=RfVariant.RTL8225Z2_B, hw_rev=HwRev.RTL8187BvE)
    b_probe = dataclasses.replace(
        _probe_result(), setup=b_setup,
        chip=ChipVariant(hwver_raw=None, name="RTL8187BvE", is_rtl8187b=True,
                         hw_rev=HwRev.RTL8187BvE))
    monkeypatch.setattr(drv, "probe", lambda t, product_id, is_rtl8187b: b_probe)
    monkeypatch.setattr(drv, "cold_bring_up", lambda t, rf_init, is_rtl8187b:
                        (calls.append(("cold_bring_up", is_rtl8187b)), 0x909CFC0B)[1])
    readers = MagicMock()
    monkeypatch.setattr(drv, "RxReaderThread", readers)

    with caplog.at_level(logging.WARNING, logger=drv.__name__):
        assert await d.connect() is True

    assert ("RTL8187B (RTL8187BvE): ported from the kernel source, untested on hardware"
            in caplog.messages)
    assert calls == [("claim",), ("cold_bring_up", True), ("config_channel", b_setup, 1, _POWER)]
    assert d._bulk_in_ep == 0x83                                   # dev.c:435
    assert d._bulk_out_max_packet_sizes == {12: 64, 6: 64, 7: 64, 5: 64, 4: 64}
    assert [c.kwargs["name"] for c in readers.call_args_list] == ["rtl8187-rx",
                                                                    "rtl8187b-tx-status"]


async def test_connect_fails_when_radio_switch_reads_off(monkeypatch):
    import dataclasses

    import pytest

    from wifit3.errors import BringUpError

    calls: list[tuple] = []
    d = _mock_bring_up(monkeypatch, calls)
    monkeypatch.setattr(drv, "probe",
                        lambda t, product_id, is_rtl8187b:
                        dataclasses.replace(_probe_result(), rfkill_off=False))

    with pytest.raises(BringUpError, match="radio switch reads OFF") as exc:
        await d.connect()
    assert exc.value.stage == "rfkill"
    assert ("cold_bring_up",) not in calls


def test_from_usb_device_reads_driver_info_from_the_id_entry():
    from wifit3.models.device_id import DeviceID

    b = drv.RTL8187Driver.from_usb_device(MagicMock(), DeviceID(0x0846, 0x4260, "RTL8187B"))
    l_ = drv.RTL8187Driver.from_usb_device(MagicMock(), DeviceID(0x0BDA, 0x8187, "RTL8187L"))
    assert b.is_rtl8187b_id and not l_.is_rtl8187b_id



async def test_connect_passes_id_product_and_driver_info_to_probe(monkeypatch):
    calls: list[tuple] = []
    d = _mock_bring_up(monkeypatch, calls)
    d.dev.idProduct = 0x8187
    seen: list[tuple] = []
    monkeypatch.setattr(drv, "probe", lambda t, product_id, is_rtl8187b:
                        (seen.append((product_id, is_rtl8187b)), _probe_result())[1])
    await d.connect()
    assert seen == [(0x8187, False)]
