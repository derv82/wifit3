from types import SimpleNamespace
from unittest.mock import MagicMock

from wifit3.models import AccessPoint
from wifit3.persist.config import Config
from wifit3.ui.screens.decloak_modal import decloak_candidate_defaults
from wifit3.ui.screens.focus_v2.screen import FocusViewV2


def test_remembered_ssid_is_the_first_default_without_being_assigned_to_the_ap():
    hidden = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6)
    sibling = AccessPoint(bssid="11:22:33:44:55:66", ssid="Home", channel=6)
    sibling.beacons = 10
    hidden.siblings = [sibling.bssid]
    array = SimpleNamespace(access_points={sibling.bssid: sibling})
    Config.decloaked_ssids = {hidden.bssid: "Remembered"}

    base, candidates = decloak_candidate_defaults(array, hidden)

    assert base == "Home"
    assert candidates[0] == "Remembered"
    assert "Home" in candidates
    assert hidden.ssid is None and hidden.decloak_method is None


def test_focus_logs_the_remembered_candidate_before_reconfirming_it():
    hidden = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6)
    Config.decloaked_ssids = {hidden.bssid: "Remembered"}
    controls = MagicMock()
    controls.start.return_value = object()
    screen = SimpleNamespace(
        _target_ap=hidden,
        app=SimpleNamespace(array=object()),
        _controls=controls,
        _log=MagicMock(),
        refresh_buttons=MagicMock(),
    )

    FocusViewV2._on_decloak_candidates(screen, ["Remembered", "Other"])

    assert 'Decloaking aa:bb:cc:dd:ee:ff ("Remembered")...' in screen._log.call_args_list[0].args[0]
