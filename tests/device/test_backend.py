"""The USB backend interface: one libusb context."""
import usb.core

from wifit3.device import backend


def test_libusb_backend_enumerates(monkeypatch):
    dev = object()
    monkeypatch.setattr(usb.core, "find", lambda **kw: [dev])
    assert backend.LibusbBackend(object()).enumerate() == [dev]


def test_libusb_backend_find_targets_instance(monkeypatch):
    seen = {}
    dev = object()

    def find(**kw):
        seen.update(kw)
        return dev

    monkeypatch.setattr(usb.core, "find", find)
    ctx = object()
    assert backend.LibusbBackend(ctx).find(0x1111, 0x2222, bus=2, address=7) is dev
    assert seen == {"idVendor": 0x1111, "idProduct": 0x2222, "backend": ctx, "bus": 2, "address": 7}


def test_libusb_backend_find_without_instance(monkeypatch):
    seen = {}

    def find(**kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(usb.core, "find", find)
    backend.LibusbBackend(object()).find(1, 2)
    assert "bus" not in seen and "address" not in seen


def test_libusb_backend_find_all(monkeypatch):
    devs = [object()]
    monkeypatch.setattr(usb.core, "find", lambda **kw: devs)
    assert backend.LibusbBackend(object()).find_all(1, 2) == devs


def test_libusb_backend_node_path():
    dev = type("D", (), {"bus": 2, "address": 35})()
    assert backend.LibusbBackend(None).node_path(dev) == "/dev/bus/usb/002/035"


def test_libusb_backend_node_path_unknown():
    assert backend.LibusbBackend(None).node_path(object()) is None


def test_usb_backend_returns_a_libusb_backend(monkeypatch):
    low = object()
    monkeypatch.setattr(backend, "_desktop_libusb", lambda: low)
    assert isinstance(backend.usb_backend(), backend.LibusbBackend)


def test_usb_backend_missing_libusb(monkeypatch):
    monkeypatch.setattr(backend, "_desktop_libusb", lambda: None)
    assert backend.usb_backend() is None
