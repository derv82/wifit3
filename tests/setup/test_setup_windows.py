"""SetupWindows install/uninstall; the elevated calls are stubbed (see test_windows.py)."""
from dataclasses import replace

import wifit3.setup.windows as win
from wifit3.chips.driver import DeviceID
from wifit3.setup.base import SetupResult

_DEV = DeviceID(0x0BDA, 0x8813, "RTL8814AU (Alfa AWUS1900)")


class FakePrompter:
    def __init__(self, ask=True):
        self._ask = ask
        self.statuses, self.errors = [], []

    async def ask(self, dialog):
        return self._ask

    async def wait_replug(self, device_id):
        return True

    def status(self, message):
        self.statuses.append(message)

    def error(self, title, body):
        self.errors.append((title, body))

    def begin_assistant(self, greeting, messages, *, intro_delay=2.0):
        self.assistant = "begun"

    async def end_assistant(self, ok):
        self.assistant = ("ended", ok)


class _Install:
    def __init__(self, ok=True, message="", cancelled=False, wdi_code=None, detail=None):
        self.ok, self.message, self.cancelled = ok, message, cancelled
        self.wdi_code, self.detail = wdi_code, detail


class _Restore:
    def __init__(self, ok=True, message="", cancelled=False, detail=None):
        self.ok, self.message, self.cancelled, self.detail = ok, message, cancelled, detail


async def test_install_declined_runs_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(win, "install_winusb", lambda *a, **k: called.append(1) or _Install())
    assert await win.SetupWindows().install(_DEV, FakePrompter(ask=False)) is None
    assert called == []


async def test_install_returns_device_at_new_address(monkeypatch):
    # WinUSB may re-enumerate the device to a new address; install finds it again and returns it.
    live = replace(_DEV, bus=2, address=64)
    monkeypatch.setattr(win, "install_winusb", lambda *a, **k: _Install(ok=True))
    monkeypatch.setattr(win, "find_device", lambda dev: live)
    assert await win.SetupWindows().install(_DEV, FakePrompter()) is live


async def test_install_falls_back_when_device_not_found(monkeypatch):
    # find_device returns None (device not on the bus): fall back to the original device_id.
    monkeypatch.setattr(win, "install_winusb", lambda *a, **k: _Install(ok=True))
    monkeypatch.setattr(win, "find_device", lambda dev: None)
    assert await win.SetupWindows().install(_DEV, FakePrompter()) is _DEV


async def test_install_failure_reports_code_and_detail(monkeypatch):
    monkeypatch.setattr(win, "install_winusb", lambda *a, **k: _Install(
        ok=False, message="Windows refused the unsigned driver package.", wdi_code=-19, detail="bad inf"))
    ui = FakePrompter()
    assert await win.SetupWindows().install(_DEV, ui) is None
    assert ui.errors and "libwdi code -19" in ui.errors[0][1] and "bad inf" in ui.errors[0][1]


async def test_install_cancelled_shows_no_error(monkeypatch):
    monkeypatch.setattr(win, "install_winusb", lambda *a, **k: _Install(ok=False, cancelled=True))
    ui = FakePrompter()
    assert await win.SetupWindows().install(_DEV, ui) is None
    assert ui.errors == []


async def test_uninstall_cancelled(monkeypatch):
    monkeypatch.setattr(win, "restore_driver", lambda v, p: _Restore(ok=True))
    res = await win.SetupWindows().uninstall(_DEV, FakePrompter(ask=None))
    assert res.cancelled and not res.ok


async def test_uninstall_success(monkeypatch):
    monkeypatch.setattr(win, "restore_driver", lambda v, p: _Restore(ok=True, message="removed"))
    res = await win.SetupWindows().uninstall(_DEV, FakePrompter(ask="narrow"))
    assert isinstance(res, SetupResult) and res.ok and res.message == "removed"
