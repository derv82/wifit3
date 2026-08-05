"""DeviceWatch: the multiset diff and poll's change / pause / fatal behaviour."""
from wifit3.chips.driver import DeviceID
from wifit3.device.watch import DeviceWatch, _diff
from wifit3.errors import WifiteFatalError

# Live instances as devices() returns them: tagged with a (bus, address). A and A2 are the same
# model on two ports (a real twin pair); B is a different card.
A = DeviceID(0x0BDA, 0x8813, "RTL8814AU", bus=1, address=4)
A2 = DeviceID(0x0BDA, 0x8813, "RTL8814AU", bus=1, address=5)
B = DeviceID(0x148F, 0x5370, "RT5370", bus=1, address=6)


class _DM:
    """A DeviceManager stand-in whose devices() returns a fixed list (or raises)."""
    def __init__(self, devs):
        self._devs = devs

    def devices(self):
        if isinstance(self._devs, Exception):
            raise self._devs
        return self._devs


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


async def test_poll_fires_on_change_then_stays_quiet():
    events = []
    watch = DeviceWatch(_DM([A]), on_change=lambda c, a, d: events.append((c, a, d)))
    await watch.poll()
    assert events == [([A], [A], [])] and watch.present() == [A]
    await watch.poll()                             # unchanged -> no second event
    assert len(events) == 1


async def test_poll_paused_is_noop():
    events = []
    watch = DeviceWatch(_DM([A]), on_change=lambda c, a, d: events.append(1))
    watch.pause()
    await watch.poll()
    assert events == []
    watch.resume()
    await watch.poll()
    assert events == [1]


async def test_poll_fatal_reports_once_then_stops():
    fatals = []
    watch = DeviceWatch(_DM(WifiteFatalError("USB backend unavailable", "no libusb")),
                        on_change=lambda *a: None, on_fatal=fatals.append)
    await watch.poll()
    await watch.poll()
    assert len(fatals) == 1
