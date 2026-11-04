"""Termux setup: Android has no udev/modprobe step; point the user at the startup prompt."""
from wifit3.models import DeviceID
from wifit3.setup.termux import SetupTermux


class _Prompter:
    def __init__(self):
        self.errors = []

    async def ask(self, dialog):
        return True

    async def wait_replug(self, device_id):
        return device_id

    def status(self, message):
        pass

    def error(self, title, body):
        self.errors.append((title, body))

    async def run_with_terminal(self, run):
        return None


_DEV = DeviceID(0x2357, 0x0120, "Archer T2U+")


async def test_install_points_at_the_launcher():
    ui = _Prompter()
    assert await SetupTermux().install(_DEV, ui) is None
    assert ui.errors and "wifit3 --list-usb" in ui.errors[0][1]


async def test_uninstall_is_a_no_op():
    res = await SetupTermux().uninstall(_DEV, _Prompter())
    assert res.ok and not res.cancelled
