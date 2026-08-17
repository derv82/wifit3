import re

from wifit3.ui.screens.focus_v2.eviltwin_modal import _plus_one, _random_bssid, _CYCLES

_MAC = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")


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
