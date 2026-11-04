"""The USB backend interface: desktop libusb and Termux's aggregated contexts."""
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


def test_usb_backend_termux(monkeypatch):
    monkeypatch.setattr(backend, "is_termux", lambda: True)
    assert isinstance(backend.usb_backend(), backend.TermuxBackend)


def test_usb_backend_desktop(monkeypatch):
    monkeypatch.setattr(backend, "is_termux", lambda: False)
    low = object()
    monkeypatch.setattr(backend, "_desktop_libusb", lambda: low)
    assert isinstance(backend.usb_backend(), backend.LibusbBackend)


def test_usb_backend_desktop_missing_libusb(monkeypatch):
    monkeypatch.setattr(backend, "is_termux", lambda: False)
    monkeypatch.setattr(backend, "_desktop_libusb", lambda: None)
    assert backend.usb_backend() is None


def test_termux_backend_enumerates(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("wifit3.device.termux_usb.enumerate_devices", lambda: [sentinel])
    assert backend.TermuxBackend().enumerate() == [sentinel]


def test_termux_backend_finds(monkeypatch):
    monkeypatch.setattr("wifit3.device.termux_usb.find_device",
                        lambda vid, pid, bus, address: (vid, pid, bus, address))
    assert backend.TermuxBackend().find(1, 2, bus=3, address=4) == (1, 2, 3, 4)


def test_termux_backend_finds_all(monkeypatch):
    monkeypatch.setattr("wifit3.device.termux_usb.find_all_devices", lambda vid, pid: [(vid, pid)])
    assert backend.TermuxBackend().find_all(1, 2) == [(1, 2)]


def test_desktop_backend_has_no_status_hint():
    assert backend.LibusbBackend(None).status_hint() is None


def test_termux_backend_reports_the_last_error(monkeypatch):
    monkeypatch.setattr("wifit3.device.termux_usb.last_error", lambda: "boom")
    assert backend.TermuxBackend().status_hint() == "boom"
