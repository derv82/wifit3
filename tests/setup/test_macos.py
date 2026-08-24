"""SetupMacOS: install is a bare retry for chips macOS does not bind; chips the kernel binds
itself (Broadcom USB, ``MACOS_KERNEL_BINDS_CHIP``) get tailored messaging."""
from wifit3.chips.driver import DeviceID
from wifit3.setup import macos as macos_module
from wifit3.setup.base import SetupResult
from wifit3.setup.macos import SetupMacOS


class _FakePrompter:
    """Structural Prompter for tests: canned answers, no Textual."""
    async def ask(self, dialog):
        return True

    async def wait_replug(self, device_id):
        return True

    def status(self, message):
        pass

    def error(self, title, body):
        pass


_DEV = DeviceID(0x0BDA, 0x8813, "Test card")
_BCM = DeviceID(0x0A5C, 0x21F1, "Broadcom card")


class _KernelBoundDriver:
    """A driver that declares the macOS kernel binds its chipset."""
    MACOS_KERNEL_BINDS_CHIP = True


def _record_status(prompter):
    """Return the prompter with ``status`` recording into a list."""
    seen = []
    prompter.status = seen.append
    return prompter, seen


async def test_macos_install_returns_the_card_for_a_retry():
    assert await SetupMacOS().install(_DEV, _FakePrompter()) is _DEV


async def test_macos_uninstall_reports_nothing_to_remove():
    res = await SetupMacOS().uninstall(_DEV, _FakePrompter())
    assert isinstance(res, SetupResult) and res.ok and not res.cancelled


async def test_kernel_bound_chip_install_warns_and_still_retries(monkeypatch):
    prompter, seen = _record_status(_FakePrompter())
    monkeypatch.setattr(macos_module, "driver_for", lambda vid, pid: (_KernelBoundDriver, "k"))
    assert await SetupMacOS().install(_BCM, prompter) is _BCM
    assert "built-in macOS driver" in seen[0]


async def test_kernel_bound_chip_uninstall_explains_no_userland_unbind(monkeypatch):
    monkeypatch.setattr(macos_module, "driver_for", lambda vid, pid: (_KernelBoundDriver, "k"))
    res = await SetupMacOS().uninstall(_BCM, _FakePrompter())
    assert res.ok and "kext" in res.message


async def test_unregistered_chip_falls_back_to_noop_messaging(monkeypatch):
    prompter, seen = _record_status(_FakePrompter())
    monkeypatch.setattr(macos_module, "driver_for", lambda vid, pid: None)
    assert await SetupMacOS().install(_DEV, prompter) is _DEV
    assert "no driver setup" in seen[0]