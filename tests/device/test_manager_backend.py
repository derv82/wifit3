"""manager delegates enumeration and lookup to the USB backend."""
import pytest

import wifit3.device.manager as manager
from tests.device.fakes import FakeBackend
from wifit3.chips.driver import DeviceID


def _dev(vid=0x148F, pid=0x5370, bus=1, address=2):
    return type("D", (), {"idVendor": vid, "idProduct": pid,
                          "bus": bus, "address": address})()


def test_devices_empty(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", lambda: FakeBackend())
    assert manager.devices() == []


def test_devices_uses_the_backend(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", lambda: FakeBackend([_dev()]))
    out = manager.devices()
    assert [d.chipset for d in out] == ["RT5370"]
    assert (out[0].bus, out[0].address) == (1, 2)


def test_linux_node_path_uses_the_backend(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", lambda: FakeBackend([_dev()]))
    entry = DeviceID(0x148F, 0x5370, "RT5370", bus=1, address=2)
    assert manager.linux_node_path(entry) == "/dev/bus/usb/001/002"


def test_raw_devices_raises_without_a_backend(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", lambda: None)
    with pytest.raises(manager.WifiteFatalError):
        manager.raw_devices()


def test_linux_node_path_raises_without_a_backend(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", lambda: None)
    with pytest.raises(manager.WifiteFatalError):
        manager.linux_node_path(DeviceID(0x148F, 0x5370, "RT5370", bus=1, address=2))
