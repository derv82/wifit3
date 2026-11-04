"""wifit3's Termux startup: the CLI flags and the system-libusb check."""
import os
import sys

import pytest

import wifit3.__main__ as entry
from wifit3.device import termux_usb


class _App:
    ran = False

    def __init__(self, cli_log_level=None):
        self.cli_log_level = cli_log_level

    def run(self):
        _App.ran = True


@pytest.fixture(autouse=True)
def _termux(monkeypatch):
    _App.ran = False
    monkeypatch.setattr("wifit3.host.is_termux", lambda: True)
    monkeypatch.setattr("usb.backend.libusb1.get_backend", lambda: object())
    monkeypatch.setattr("wifit3.ui.app.WifiteApp", _App)
    saved = {name: os.environ.pop(name, None) for name in ("TERMUX_USB_FD",)}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["wifit3", *args])
    entry.main()


def test_main_runs_the_app(monkeypatch):
    _run(monkeypatch)
    assert _App.ran


def test_main_requires_system_libusb(monkeypatch):
    monkeypatch.setattr("usb.backend.libusb1.get_backend", lambda: None)

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch)

    assert exc.value.code == 1
    assert not _App.ran


def test_list_usb_prints_paths(monkeypatch, capsys):
    monkeypatch.setattr(termux_usb, "list_devices", lambda: ["/dev/bus/usb/001/002"])

    _run(monkeypatch, "--list-usb")

    assert capsys.readouterr().out.strip() == "/dev/bus/usb/001/002"
    assert not _App.ran


def test_list_usb_explains_an_empty_bus(monkeypatch):
    monkeypatch.setattr(termux_usb, "list_devices", lambda: [])

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--list-usb")

    assert exc.value.code == 1


def test_list_usb_is_rejected_on_desktop(monkeypatch):
    monkeypatch.setattr("wifit3.host.is_termux", lambda: False)
    monkeypatch.setattr(sys, "argv", ["wifit3", "--list-usb"])

    with pytest.raises(SystemExit) as exc:
        entry.main()

    assert exc.value.code == 2
