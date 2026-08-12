"""SetupLinux orchestration: the confirm -> install_rule -> replug -> wait sequence, and the
uninstall radii. The privileged pieces (install_rule, remove_rule, plan_uninstall, node lookup) are
stubbed here since test_linux.py covers their internals; these tests pin the sequence SetupLinux
drives and how it turns each outcome into a bool / SetupResult, with a FakePrompter and no hardware.
"""
from dataclasses import replace

import pytest

import wifit3.setup.linux as lin
from wifit3.chips.driver import DeviceID
from wifit3.setup import SetupTarget

_DEV = DeviceID(0x0BDA, 0x8813, "RTL8814AU (Alfa AWUS1900)", bus=1, address=5)


def _target(*, replug=False):
    return SetupTarget(key="rtl8814au", description="RTL8814AU", ids=((0x0BDA, 0x8813),),
                       module_hints=("8814au",), replug_after_modprobe=replug)


class FakePrompter:
    def __init__(self, ask=True, replug=True):
        self._ask, self._replug = ask, replug
        self.statuses, self.errors = [], []

    async def ask(self, dialog):
        return self._ask

    async def wait_replug(self, device_id):
        return replace(device_id, address=99) if self._replug else None

    def status(self, message):
        self.statuses.append(message)

    def error(self, title, body):
        self.errors.append((title, body))


class _Result:
    """LinuxSetupResult stand-in for the install_rule / remove_rule stubs."""
    def __init__(self, ok=True, message="", cancelled=False, detail=None):
        self.ok, self.message, self.cancelled, self.detail = ok, message, cancelled, detail


class _Plan:
    def __init__(self, removable=True, siblings=(), has_own=True):
        self._removable, self.siblings, self.has_own_files = removable, list(siblings), has_own

    @property
    def removable(self):
        return self._removable


class _Sib:
    def __init__(self, key, description):
        self.key, self.description = key, description


async def _access_ok(self, device_id, *, writable, timeout=5.0, interval=0.1):
    return True


async def _access_never(self, device_id, *, writable, timeout=5.0, interval=0.1):
    return False


@pytest.fixture(autouse=True)
def _stub_platform_bits(monkeypatch):
    # current_user() does `import pwd` (Unix-only); usb_node_path enumerates the bus. Stub both so
    # the orchestration runs on any host.
    monkeypatch.setattr(lin, "current_user", lambda: "tester")
    monkeypatch.setattr(lin, "usb_node_path", lambda dev: "/dev/bus/usb/001/005")


# ---- install ------------------------------------------------------------------------------------

async def test_install_declined_writes_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target())
    monkeypatch.setattr(lin, "install_rule", lambda *a, **k: called.append(1) or _Result())
    assert await lin.SetupLinux().install(_DEV, FakePrompter(ask=False)) is None
    assert called == []


async def test_install_happy_no_replug(monkeypatch):
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target(replug=False))
    monkeypatch.setattr(lin, "install_rule", lambda *a, **k: _Result(ok=True))
    monkeypatch.setattr(lin.SetupLinux, "_wait_for_access", _access_ok)
    assert await lin.SetupLinux().install(_DEV, FakePrompter()) is _DEV


async def test_install_returns_the_replugged_card(monkeypatch):
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target(replug=True))
    monkeypatch.setattr(lin, "install_rule", lambda *a, **k: _Result(ok=True))
    monkeypatch.setattr(lin.SetupLinux, "_wait_for_access", _access_ok)
    got = await lin.SetupLinux().install(_DEV, FakePrompter(replug=True))
    assert got is not None and got.address == 99   # the re-addressed card, not the pre-unplug one


async def test_install_replug_skipped_returns_none(monkeypatch):
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target(replug=True))
    monkeypatch.setattr(lin, "install_rule", lambda *a, **k: _Result(ok=True))
    assert await lin.SetupLinux().install(_DEV, FakePrompter(replug=False)) is None


async def test_install_rule_failure_reports_error(monkeypatch):
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target())
    monkeypatch.setattr(lin, "install_rule", lambda *a, **k: _Result(ok=False, message="boom"))
    ui = FakePrompter()
    assert await lin.SetupLinux().install(_DEV, ui) is None
    assert ui.errors and ui.errors[0][0] == "Couldn't install the device rules"


async def test_install_cancelled_elevation_shows_no_error(monkeypatch):
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target())
    monkeypatch.setattr(lin, "install_rule", lambda *a, **k: _Result(ok=False, cancelled=True))
    ui = FakePrompter()
    assert await lin.SetupLinux().install(_DEV, ui) is None
    assert ui.errors == []


async def test_install_access_never_takes_effect(monkeypatch):
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target())
    monkeypatch.setattr(lin, "install_rule", lambda *a, **k: _Result(ok=True))
    monkeypatch.setattr(lin.SetupLinux, "_wait_for_access", _access_never)
    ui = FakePrompter()
    assert await lin.SetupLinux().install(_DEV, ui) is None
    assert ui.errors and "didn't take effect" in ui.errors[0][0]


async def test_install_unsupported_chipset(monkeypatch):
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: None)
    ui = FakePrompter()
    assert await lin.SetupLinux().install(_DEV, ui) is None
    assert ui.errors


# ---- uninstall ----------------------------------------------------------------------------------

async def test_uninstall_nothing_installed(monkeypatch):
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target())
    monkeypatch.setattr(lin, "plan_uninstall", lambda t: _Plan(removable=False))
    res = await lin.SetupLinux().uninstall(_DEV, FakePrompter())
    assert res.ok and "No wifit3 rules" in res.message


async def test_uninstall_cancelled(monkeypatch):
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target())
    monkeypatch.setattr(lin, "plan_uninstall", lambda t: _Plan())
    res = await lin.SetupLinux().uninstall(_DEV, FakePrompter(ask=None))
    assert res.cancelled and not res.ok


async def test_uninstall_narrow_removes_only_self(monkeypatch):
    seen = {}
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target())
    monkeypatch.setattr(lin, "plan_uninstall", lambda t: _Plan(siblings=[_Sib("rt3070", "RT3070")]))
    monkeypatch.setattr(lin, "remove_rule",
                        lambda t, *, node, also_keys: seen.update(also=also_keys) or _Result(ok=True, message="removed"))
    monkeypatch.setattr(lin.SetupLinux, "_wait_for_access", _access_ok)
    res = await lin.SetupLinux().uninstall(_DEV, FakePrompter(ask="narrow"))
    assert res.ok and seen["also"] == ()


async def test_uninstall_wide_passes_sibling_keys(monkeypatch):
    seen = {}
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target())
    monkeypatch.setattr(lin, "plan_uninstall", lambda t: _Plan(siblings=[_Sib("rt3070", "RT3070")]))
    monkeypatch.setattr(lin, "remove_rule",
                        lambda t, *, node, also_keys: seen.update(also=also_keys) or _Result(ok=True, message="removed"))
    monkeypatch.setattr(lin.SetupLinux, "_wait_for_access", _access_ok)
    res = await lin.SetupLinux().uninstall(_DEV, FakePrompter(ask="wide"))
    assert res.ok and seen["also"] == ("rt3070",)


async def test_uninstall_not_revoked_message(monkeypatch):
    monkeypatch.setattr(lin, "target_for_vidpid", lambda v, p: _target())
    monkeypatch.setattr(lin, "plan_uninstall", lambda t: _Plan())
    monkeypatch.setattr(lin, "remove_rule", lambda *a, **k: _Result(ok=True, message="removed"))
    monkeypatch.setattr(lin.SetupLinux, "_wait_for_access", _access_never)
    res = await lin.SetupLinux().uninstall(_DEV, FakePrompter(ask="narrow"))
    assert res.ok and "fully revoke access" in res.message


async def test_wait_for_access_short_circuits_for_root(monkeypatch):
    # os.access ignores perm bits for root, so waiting for a revoke would spin the full timeout;
    # under root either target state is treated as already reached.
    monkeypatch.setattr(lin.os, "geteuid", lambda: 0)
    assert await lin.SetupLinux()._wait_for_access(_DEV, writable=False) is True
    assert await lin.SetupLinux()._wait_for_access(_DEV, writable=True) is True
