"""discovery.wait_for_presence: the unplug/replug poll behind the replug modal. It reads the cheap
find_devices() enumeration (no interface rebuild), so we drive it with a scripted device list."""
import wifit3.wlan.discovery as discovery
from wifit3.chips.driver import DeviceID

_DEV = DeviceID(0x148F, 0x5370, "RT5370")


def _scripted_find(monkeypatch, frames):
    """find_devices() yields each frame (a list of DeviceIDs) in turn, then repeats the last."""
    seq = list(frames)

    def _find():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(discovery, "find_devices", _find)


async def test_returns_true_when_present_reached(monkeypatch):
    _scripted_find(monkeypatch, [[], [], [_DEV]])   # absent, absent, then present
    ok = await discovery.wait_for_presence(_DEV.vid, _DEV.pid, present=True, timeout=1.0, interval=0)
    assert ok is True


async def test_returns_true_when_absent_reached(monkeypatch):
    _scripted_find(monkeypatch, [[_DEV], []])        # present, then unplugged
    ok = await discovery.wait_for_presence(_DEV.vid, _DEV.pid, present=False, timeout=1.0, interval=0)
    assert ok is True


async def test_times_out_when_state_never_reached(monkeypatch):
    monkeypatch.setattr(discovery, "find_devices", lambda: [])   # never appears
    ok = await discovery.wait_for_presence(_DEV.vid, _DEV.pid, present=True, timeout=0.0, interval=0)
    assert ok is False
