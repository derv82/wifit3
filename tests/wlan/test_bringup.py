"""BringupManager.run: connect-first, then setup-on-permission-failure, then retry. The engine is
exercised headless with a fake app / setup / prompter and a stubbed discovery layer; the only real
collaborator is WlanArray (the card really gets pooled)."""
import wifit3.wlan.bringup as bringup
from wifit3.chips.driver import DeviceID
from wifit3.errors import BringUpError, BringUpPermissionsError
from wifit3.setup.base import SetupResult
from wifit3.wlan.bringup import BringupManager, Status

_DEV = DeviceID(0x0BDA, 0x8813, "RTL8814AU (Alfa AWUS1900)")


class FakeInterface:
    supported_channels = [1, 6, 11]

    def __init__(self, *, exc=None, ok=True):
        self.vid, self.pid, self.description = _DEV.vid, _DEV.pid, _DEV.description
        self.name = "wlan?"
        self.on_tx = None
        self._exc, self._ok = exc, ok

    def register_rx_callback(self, cb):
        pass

    def register_disconnect_callback(self, cb):
        pass

    async def connect(self, progress_cb=None):
        if self._exc is not None:
            raise self._exc
        return self._ok


class FakeApp:
    def __init__(self):
        self.array = None
        self.lost = []
        self.toasts = []

    def notify_device_lost(self, exc, remaining):
        self.lost.append((exc, remaining))

    def notify(self, msg, timeout=None):
        self.toasts.append(msg)


class FakePrompter:
    def __init__(self):
        self.opened = None
        self.closed = False
        self.statuses = []
        self.errors = []

    async def open(self, title):
        self.opened = title

    def close(self):
        self.closed = True

    def status(self, message):
        self.statuses.append(message)

    def status_progress(self, fraction, message):
        self.statuses.append(message)

    async def ask(self, dialog):
        return True

    async def wait_replug(self, device_id):
        return True

    def error(self, title, body):
        self.errors.append((title, body))


class FakeSetup:
    def __init__(self, install=True):
        self._install = install
        self.installed = 0

    async def install(self, device_id, ui):
        self.installed += 1
        return self._install

    async def uninstall(self, device_id, ui):
        return SetupResult(ok=True, message="removed")


def _queue_builds(monkeypatch, *ifaces):
    q = list(ifaces)

    def _build(device_id, name="wlan0"):
        if not q:
            return None
        iface = q.pop(0)
        iface.name = name
        return iface

    monkeypatch.setattr(bringup, "build_interface", _build)
    monkeypatch.setattr(bringup, "find_devices", lambda: [_DEV])   # only the primary is present


def _mgr(app=None, setup=None):
    return BringupManager(app or FakeApp(), setup=setup or FakeSetup(), prompter=FakePrompter())


async def test_run_success_pools_card(monkeypatch):
    _queue_builds(monkeypatch, FakeInterface(ok=True))
    app = FakeApp()
    bm = _mgr(app)
    res = await bm.run(_DEV)
    assert res.status is Status.READY
    assert app.array is not None and len(app.array.members) == 1
    assert bm.prompter.opened and bm.prompter.closed


async def test_permission_failure_then_install_succeeds(monkeypatch):
    _queue_builds(monkeypatch, FakeInterface(exc=BringUpPermissionsError("open", "no winusb")),
                  FakeInterface(ok=True))
    setup = FakeSetup(install=True)
    bm = _mgr(setup=setup)
    res = await bm.run(_DEV)
    assert res.status is Status.READY and setup.installed == 1


async def test_permission_failure_install_declined(monkeypatch):
    _queue_builds(monkeypatch, FakeInterface(exc=BringUpPermissionsError("open", "no winusb")))
    bm = _mgr(setup=FakeSetup(install=False))
    res = await bm.run(_DEV)
    assert res.status is Status.CANCELLED


async def test_hard_fault_is_failed_not_setup(monkeypatch):
    _queue_builds(monkeypatch, FakeInterface(exc=BringUpError("firmware", "FW timeout")))
    setup = FakeSetup()
    bm = _mgr(setup=setup)
    res = await bm.run(_DEV)
    assert res.status is Status.FAILED and "firmware" in res.message and setup.installed == 0


async def test_second_connect_fault_after_install(monkeypatch):
    _queue_builds(monkeypatch, FakeInterface(exc=BringUpPermissionsError("open", "x")),
                  FakeInterface(exc=BringUpError("init", "no RX")))
    res = await _mgr(setup=FakeSetup(install=True)).run(_DEV)
    assert res.status is Status.FAILED and "init" in res.message


async def test_card_absent_is_failed(monkeypatch):
    _queue_builds(monkeypatch)   # build_interface returns None
    res = await _mgr().run(_DEV)
    assert res.status is Status.FAILED


async def test_modal_closed_even_on_failure(monkeypatch):
    _queue_builds(monkeypatch, FakeInterface(exc=BringUpError("firmware", "x")))
    bm = _mgr()
    await bm.run(_DEV)
    assert bm.prompter.closed


async def test_uninstall_delegates_to_setup(monkeypatch):
    res = await _mgr().uninstall(_DEV)
    assert isinstance(res, SetupResult) and res.ok and res.message == "removed"
