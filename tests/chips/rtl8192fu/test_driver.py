"""RTL8192FUDriver lifecycle wiring and the USB endpoint parse."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import usb.util

import wifit3.chips.rtl8192fu.driver as drv
from wifit3.chips.rtl8192fu import constants as C
from wifit3.chips.rtl8192fu.core import Nl80211Iftype, Rtl8xxxuPriv
from wifit3.chips.rtl8192fu.rtl8192f import rtl8192fu_fops
from wifit3.chips.rtl8192fu.transport import RTL8192FUTransport

from .fake import FakeTransport


def _driver(monkeypatch, calls: list[str]) -> drv.RTL8192FUDriver:
    driver = drv.RTL8192FUDriver(MagicMock(), MagicMock())
    monkeypatch.setattr(driver.transport, "claim", lambda: calls.append("claim"))
    monkeypatch.setattr(driver.transport, "release", lambda: calls.append("release"))
    monkeypatch.setattr(driver.transport, "interface_number", 0)
    monkeypatch.setattr(driver.transport, "rtl8xxxu_parse_usb", lambda priv: calls.append("parse_usb"))
    monkeypatch.setattr(drv, "rtl8xxxu_probe", lambda priv, fw, progress: calls.append("probe"))
    monkeypatch.setattr(drv, "start_monitor_interface", lambda priv: calls.append("start_monitor"))
    reader = MagicMock()
    reader.stop = MagicMock(side_effect=lambda: _done(calls, "reader_stop"))
    monkeypatch.setattr(drv, "RxReaderThread", lambda *a, **k: reader)
    reader.start.side_effect = lambda: calls.append("reader_start")
    return driver


async def _done(calls: list[str], name: str) -> None:
    calls.append(name)


async def test_connect_probes_then_starts_monitor_rx_and_watchdog(monkeypatch):
    calls: list[str] = []
    driver = _driver(monkeypatch, calls)
    assert await driver.connect()
    assert calls == ["claim", "parse_usb", "probe", "start_monitor", "reader_start"]
    assert driver._watchdog_task is not None and not driver._watchdog_task.done()
    await driver._stop_tasks()


async def test_set_channel_tunes_once_and_skips_current(monkeypatch):
    calls: list[str] = []
    driver = _driver(monkeypatch, calls)
    await driver.connect()
    tuned: list[int] = []
    monkeypatch.setattr(drv, "rtl8xxxu_config", lambda priv, channel: tuned.append(channel))
    await driver.set_channel(6)
    await driver.set_channel(6)
    assert tuned == [6]
    assert driver.current_channel == 6
    await driver._stop_tasks()


async def test_close_runs_disconnect_order_and_releases(monkeypatch):
    calls: list[str] = []
    driver = _driver(monkeypatch, calls)
    await driver.connect()
    monkeypatch.setattr(drv, "rtl8xxxu_deinit_led", lambda priv: calls.append("deinit_led"))
    monkeypatch.setattr(drv, "rtl8xxxu_stop", lambda priv: calls.append("stop"))
    driver.priv.fops = replace(rtl8192fu_fops, power_off=lambda priv: calls.append("power_off"))
    calls.clear()
    await driver.close()
    assert calls == ["reader_stop", "deinit_led", "stop", "power_off", "release"]


async def test_enter_active_monitor_programs_port0(monkeypatch):
    driver = drv.RTL8192FUDriver(MagicMock(), MagicMock())
    transport = FakeTransport({C.REG_MSR: 0x0C})
    driver.priv.transport = transport
    mac, bssid = bytes.fromhex("020000000001"), bytes.fromhex("020000000002")

    await driver.enter_active_monitor(mac, bssid)
    writes = transport.writes()
    assert writes[0] == (C.REG_MSR, 1, 0x0C | C.MSR_LINKTYPE_STATION)
    assert [value for _, _, value in writes[1:7]] == list(mac)
    assert [address for address, _, _ in writes[1:7]] == [C.REG_MACID + i for i in range(6)]
    assert [value for _, _, value in writes[7:13]] == list(bssid)
    assert [address for address, _, _ in writes[7:13]] == [C.REG_BSSID + i for i in range(6)]

    transport.ops.clear()
    await driver.enter_active_monitor(mac, mac)
    assert transport.writes()[0] == (C.REG_MSR, 1, 0x0C | C.MSR_LINKTYPE_AP)
    assert driver.priv.vif_type == Nl80211Iftype.NL80211_IFTYPE_AP


def test_stamp_tx_seq_increments_and_keeps_fragment():
    driver = drv.RTL8192FUDriver(MagicMock(), MagicMock())
    frame = bytes(22) + bytes([0x03, 0x00])
    first, second = driver._stamp_tx_seq(frame), driver._stamp_tx_seq(frame)
    assert int.from_bytes(first[22:24], "little") == 0x0003
    assert int.from_bytes(second[22:24], "little") == 0x0013


def test_parse_usb_fills_pipes_in_descriptor_order():
    def endpoint(address: int, xtype: int) -> SimpleNamespace:
        return SimpleNamespace(bEndpointAddress=address, bmAttributes=xtype)

    bulk, intr = usb.util.ENDPOINT_TYPE_BULK, usb.util.ENDPOINT_TYPE_INTR
    interface = [endpoint(0x84, bulk), endpoint(0x05, bulk), endpoint(0x06, bulk), endpoint(0x87, intr),
                 endpoint(0x08, bulk), endpoint(0x09, bulk), endpoint(0x0C, bulk), endpoint(0x0D, bulk)]
    priv = Rtl8xxxuPriv(FakeTransport(), rtl8192fu_fops)
    RTL8192FUTransport.rtl8xxxu_parse_usb(SimpleNamespace(interface=interface), priv)
    assert priv.pipe_in == 0x84
    assert priv.pipe_interrupt == 0x87
    assert priv.out_ep == [0x05, 0x06, 0x08, 0x09, 0x0C, 0x0D]
    assert priv.nr_out_eps == 6
