"""discovery.wait_for_departure / wait_for_arrival: the instance-aware unplug/replug poll behind the
replug modal. Driven with a scripted find_devices() list, no hardware."""
from dataclasses import replace

import wifit3.wlan.discovery as discovery
from wifit3.chips.driver import DeviceID

_DEV = DeviceID(0x148F, 0x5370, "RT5370", bus=1, address=5)


def _scripted_find(monkeypatch, frames):
    """find_devices() yields each frame (a list of DeviceIDs) in turn, then repeats the last."""
    seq = list(frames)

    def _find():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(discovery, "find_devices", _find)


async def test_departure_seen_when_instance_leaves(monkeypatch):
    _scripted_find(monkeypatch, [[_DEV], []])
    assert await discovery.wait_for_departure(_DEV, timeout=1.0, interval=0) is True


async def test_departure_seen_though_identical_sibling_stays(monkeypatch):
    sibling = replace(_DEV, address=6)
    _scripted_find(monkeypatch, [[_DEV, sibling], [sibling]])
    assert await discovery.wait_for_departure(_DEV, timeout=1.0, interval=0) is True


async def test_departure_times_out_while_present(monkeypatch):
    monkeypatch.setattr(discovery, "find_devices", lambda: [_DEV])
    assert await discovery.wait_for_departure(_DEV, timeout=0.0, interval=0) is False


async def test_arrival_returns_the_re_enumerated_card(monkeypatch):
    back = replace(_DEV, address=9)
    _scripted_find(monkeypatch, [[], [back]])
    got = await discovery.wait_for_arrival(_DEV, timeout=1.0, interval=0)
    assert got is not None and got.address == 9


async def test_arrival_matches_a_same_address_replug(monkeypatch):
    _scripted_find(monkeypatch, [[], [_DEV]])   # returns at the same (bus, address) it left from
    got = await discovery.wait_for_arrival(_DEV, timeout=1.0, interval=0)
    assert got is not None and got.instance_key == _DEV.instance_key


async def test_arrival_skips_idle_sibling(monkeypatch):
    sibling = replace(_DEV, address=6)
    _scripted_find(monkeypatch, [[sibling], [sibling, replace(_DEV, address=9)]])
    got = await discovery.wait_for_arrival(_DEV, timeout=1.0, interval=0)
    assert got.address == 9


async def test_arrival_ignores_card_present_from_the_start(monkeypatch):
    sibling = replace(_DEV, address=6)
    monkeypatch.setattr(discovery, "find_devices", lambda: [sibling])
    assert await discovery.wait_for_arrival(_DEV, timeout=0.0, interval=0) is None
