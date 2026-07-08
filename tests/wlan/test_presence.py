"""WlanDeviceManager.linux_wait_for_presence — the unplug/replug poll behind the replug modal.
Driven with a stub bus (scripted presence sequence) so no hardware or real USB scan is needed."""
from wifit3.wlan.manager import WlanDeviceManager


class _Bus:
    """Stub self: yields a scripted present/absent sequence for get_interface_by_vidpid."""
    def __init__(self, presence):
        self._presence = list(presence)

    async def refresh(self):
        pass

    def get_interface_by_vidpid(self, vid, pid):
        state = self._presence.pop(0) if len(self._presence) > 1 else self._presence[0]
        return object() if state else None


async def test_wait_for_absent_returns_true_when_card_disappears():
    bus = _Bus([True, True, False])
    ok = await WlanDeviceManager.linux_wait_for_presence(
        bus, 0x1, 0x2, present=False, timeout=5, interval=0)
    assert ok is True


async def test_wait_for_present_returns_true_when_card_reappears():
    bus = _Bus([False, True])
    ok = await WlanDeviceManager.linux_wait_for_presence(
        bus, 0x1, 0x2, present=True, timeout=5, interval=0)
    assert ok is True


async def test_wait_times_out_when_state_never_changes():
    bus = _Bus([True])   # always present; waiting for it to go absent
    ok = await WlanDeviceManager.linux_wait_for_presence(
        bus, 0x1, 0x2, present=False, timeout=0.0, interval=0)
    assert ok is False
