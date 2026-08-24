"""SetupMacOS: no privileged step on macOS. install is a bare retry (returns the device and shows
the no-op status); uninstall reports there is nothing to remove."""
from wifit3.chips.driver import DeviceID
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


_DEV = DeviceID(0x0BDA, 0x8813, "Test device")


async def test_macos_install_returns_the_device_for_a_retry():
    assert await SetupMacOS().install(_DEV, _FakePrompter()) is _DEV


async def test_macos_install_shows_the_noop_status():
    seen = []
    prompter = _FakePrompter()
    prompter.status = seen.append
    await SetupMacOS().install(_DEV, prompter)
    assert "no driver setup" in seen[0]


async def test_macos_uninstall_reports_nothing_to_remove():
    res = await SetupMacOS().uninstall(_DEV, _FakePrompter())
    assert isinstance(res, SetupResult) and res.ok and not res.cancelled
