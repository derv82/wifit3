"""manager delegates enumeration and lookup to the USB backend."""
import pytest
import usb.core

import wifit3.device.manager as manager
from tests.device.fakes import FakeBackend
from wifit3.chips.driver import DeviceID


def _dev(vid=0x148F, pid=0x5370, bus=1, address=2):
    return type("D", (), {"idVendor": vid, "idProduct": pid,
                          "bus": bus, "address": address})()


def _no_backend():
    raise usb.core.NoBackendError("No backend available")


def test_devices_empty(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", lambda: FakeBackend())
    monkeypatch.setattr("wifit3.device.windows_pnp.present_usb_ids", lambda: set())
    assert manager.devices() == []


def test_devices_uses_the_backend(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", lambda: FakeBackend([_dev()]))
    monkeypatch.setattr("wifit3.device.windows_pnp.present_usb_ids", lambda: set())
    out = manager.devices()
    assert [d.chipset for d in out] == ["RT5370"]
    assert (out[0].bus, out[0].address) == (1, 2)


def test_linux_node_path_uses_the_backend(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", lambda: FakeBackend([_dev()]))
    entry = DeviceID(0x148F, 0x5370, "RT5370", bus=1, address=2)
    assert manager.linux_node_path(entry) == "/dev/bus/usb/001/002"


def test_raw_devices_raises_without_a_backend(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", _no_backend)
    with pytest.raises(manager.WifiteFatalError):
        manager.raw_devices()


def test_linux_node_path_raises_without_a_backend(monkeypatch):
    monkeypatch.setattr(manager, "usb_backend", _no_backend)
    with pytest.raises(manager.WifiteFatalError):
        manager.linux_node_path(DeviceID(0x148F, 0x5370, "RT5370", bus=1, address=2))


async def test_restore_installed_dedupes_calls_setup_and_clears(monkeypatch):
    monkeypatch.setattr(manager, "ZeroCdEjector", lambda *a, **k: object())

    class _FakeSetup:
        def __init__(self):
            self.restored = []

        async def restore(self, device_id):
            self.restored.append(device_id)

    setup = _FakeSetup()
    dm = manager.DeviceManager(app=object(), setup=setup, prompter=object())
    a = DeviceID(0x0BDA, 0x8813, "A", bus=1, address=5)
    b = DeviceID(0x148F, 0x7601, "B", bus=1, address=6)
    dm._setup_installed = [a, a, b]
    await dm.restore_installed()
    assert setup.restored == [a, b]
    assert dm._setup_installed == []
