import re
from types import SimpleNamespace

from wifit3.ui.screens.focus_v2.eviltwin_modal import (
    EvilTwinInputModal, _plus_one, _random_bssid, _CYCLES,
)

_MAC = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


def _modal(single: bool, target) -> EvilTwinInputModal:
    m = object.__new__(EvilTwinInputModal)   # exercise the pure knob helpers without a Textual app
    m._single, m.target = single, target
    return m


def test_single_card_locks_channel_to_target_and_bumps_bssid():
    target = SimpleNamespace(channel=6, bssid="94:83:c4:8c:3f:78")
    m = _modal(True, target)
    assert m._default_channel(None) == 6
    assert m._channel_options(None) == [("CH 6", 6)]
    assert m._default_bssid() == "94:83:c4:8c:3f:79"


def test_multi_card_keeps_decoy_channel_and_target_bssid():
    target = SimpleNamespace(channel=1, bssid="94:83:c4:8c:3f:78")
    m = _modal(False, target)
    twin = SimpleNamespace(supported_channels=[1, 6, 11])
    assert m._default_channel(twin) == 6                        # CSA decoy off ch 1
    assert m._default_bssid() == "94:83:c4:8c:3f:78"


def test_plus_one_bumps_last_nibble():
    assert _plus_one("94:83:c4:8c:3f:78") == "94:83:c4:8c:3f:79"
    assert _plus_one("94:83:c4:8c:3f:7f") == "94:83:c4:8c:3f:70"   # wraps f -> 0


def test_random_bssid_is_locally_administered():
    b = _random_bssid()
    assert _MAC.match(b) and b.startswith("02:")


def test_cycle_table():
    by_label = {label: (period, once) for label, period, once in _CYCLES}
    assert by_label["Never"] == (None, False)
    assert by_label["Once"][1] is True
    assert by_label["30 seconds"] == (30.0, False)
