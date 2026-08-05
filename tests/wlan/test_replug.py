"""DeviceWatch.wait_departure / wait_arrival: the instance-aware unplug/replug poll behind the replug
modal. Driven with a scripted devices() list, no hardware."""
from dataclasses import replace

from wifit3.chips.driver import DeviceID
from wifit3.device.watch import DeviceWatch

_DEV = DeviceID(0x148F, 0x5370, "RT5370", bus=1, address=5)


class _DM:
    """devices() yields each frame (a list of DeviceIDs) in turn, then repeats the last."""
    def __init__(self, frames):
        self._seq = list(frames)

    def devices(self):
        return self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]


def _watch(frames):
    return DeviceWatch(_DM(frames), on_change=lambda *a: None)


async def test_departure_seen_when_instance_leaves():
    watch = _watch([[_DEV], []])
    assert await watch.wait_departure(_DEV, timeout=1.0, interval=0) is True


async def test_departure_seen_though_identical_sibling_stays():
    sibling = replace(_DEV, address=6)
    watch = _watch([[_DEV, sibling], [sibling]])
    assert await watch.wait_departure(_DEV, timeout=1.0, interval=0) is True


async def test_departure_times_out_while_present():
    watch = _watch([[_DEV]])
    assert await watch.wait_departure(_DEV, timeout=0.0, interval=0) is False


async def test_arrival_returns_the_re_enumerated_card():
    back = replace(_DEV, address=9)
    watch = _watch([[], [back]])
    got = await watch.wait_arrival(_DEV, timeout=1.0, interval=0)
    assert got is not None and got.address == 9


async def test_arrival_matches_a_same_address_replug():
    watch = _watch([[], [_DEV]])   # returns at the same (bus, address) it left from
    got = await watch.wait_arrival(_DEV, timeout=1.0, interval=0)
    assert got is not None and got.instance_key == _DEV.instance_key


async def test_arrival_skips_idle_sibling():
    sibling = replace(_DEV, address=6)
    watch = _watch([[sibling], [sibling, replace(_DEV, address=9)]])
    got = await watch.wait_arrival(_DEV, timeout=1.0, interval=0)
    assert got.address == 9


async def test_arrival_ignores_card_present_from_the_start():
    sibling = replace(_DEV, address=6)
    watch = _watch([[sibling]])
    assert await watch.wait_arrival(_DEV, timeout=0.0, interval=0) is None
