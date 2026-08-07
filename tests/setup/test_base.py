"""The Setup contract: NoSetup's no-ops and for_platform's dispatch."""
import sys

from wifit3.chips.driver import DeviceID
from wifit3.setup.base import NoSetup, Setup, SetupResult


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


async def test_nosetup_install_declines():
    assert await NoSetup().install(_DEV, _FakePrompter()) is None


async def test_nosetup_uninstall_reports_ok():
    res = await NoSetup().uninstall(_DEV, _FakePrompter())
    assert isinstance(res, SetupResult) and res.ok and not res.cancelled


def test_for_platform_macos(monkeypatch):
    from wifit3.setup.macos import SetupMacOS
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(Setup.for_platform(), SetupMacOS)


def test_for_platform_falls_back_to_nosetup(monkeypatch):
    monkeypatch.setattr(sys, "platform", "sunos5")
    assert isinstance(Setup.for_platform(), NoSetup)
