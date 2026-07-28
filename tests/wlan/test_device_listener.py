"""DeviceListener: the multiset diff and poll_once's change / pause / fatal behaviour."""
import wifit3.wlan.device_listener as dl
from wifit3.chips.driver import DeviceID
from wifit3.errors import WifiteFatalError
from wifit3.wlan.device_listener import DeviceListener, _diff

# Live instances as find_devices() returns them: tagged with a (bus, address). A and A2 are the same
# model on two ports (a real twin pair); B is a different card.
A = DeviceID(0x0BDA, 0x8813, "RTL8814AU", bus=1, address=4)
A2 = DeviceID(0x0BDA, 0x8813, "RTL8814AU", bus=1, address=5)
B = DeviceID(0x148F, 0x5370, "RT5370", bus=1, address=6)


def test_diff_arrival():
    assert _diff([A, B], [A]) == ([B], [])


def test_diff_departure():
    assert _diff([A], [A, B]) == ([], [B])


def test_diff_twins_are_distinct_instances():
    # Same VID:PID, different address: the second card is its own arrival, not a no-op.
    assert _diff([A, A2], [A]) == ([A2], [])


def test_diff_replug_is_departure_then_arrival():
    # A replug of the same card lands at a new address, so the old instance departs and the new
    # one arrives (the modal-on-every-replug behaviour).
    assert _diff([A2], [A]) == ([A2], [A])


def test_diff_no_change_is_order_independent():
    assert _diff([A, B], [B, A]) == ([], [])


async def test_poll_fires_on_change_then_stays_quiet(monkeypatch):
    events = []
    listener = DeviceListener(on_change=lambda c, a, d: events.append((c, a, d)))
    monkeypatch.setattr(dl, "find_devices", lambda: [A])
    await listener.poll_once()
    assert events == [([A], [A], [])] and listener.present() == [A]
    await listener.poll_once()                    # unchanged -> no second event
    assert len(events) == 1


async def test_poll_paused_is_noop(monkeypatch):
    events = []
    listener = DeviceListener(on_change=lambda c, a, d: events.append(1))
    monkeypatch.setattr(dl, "find_devices", lambda: [A])
    listener.pause()
    await listener.poll_once()
    assert events == []
    listener.resume()
    await listener.poll_once()
    assert events == [1]


async def test_poll_fatal_reports_once_then_stops(monkeypatch):
    fatals = []

    def boom():
        raise WifiteFatalError("USB backend unavailable", "no libusb")

    listener = DeviceListener(on_change=lambda *a: None, on_fatal=fatals.append)
    monkeypatch.setattr(dl, "find_devices", boom)
    await listener.poll_once()
    await listener.poll_once()
    assert len(fatals) == 1
