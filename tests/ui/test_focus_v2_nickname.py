from collections import deque
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.wlan.mocks import build_ap
from wifit3.persist.config import Config
from wifit3.persist.meta import meta_path, read_meta, update_meta
from wifit3.ui.screens.focus_v2.screen import FocusViewV2


def test_nickname_save_clear_cancel_and_immediate_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    ap = build_ap()
    router = Mock()
    host = SimpleNamespace(
        app=SimpleNamespace(target_ap=ap, push_screen=Mock()),
        query_one=Mock(return_value=router), _router_values=Mock(return_value={"essid": "updated"}),
        notify=Mock(), _nickname_key=None,
    )
    FocusViewV2.action_set_nickname(host)
    modal, save = host.app.push_screen.call_args.args
    assert modal._nickname == ""
    save(None)
    assert not meta_path(ap.ssid, ap.bssid).exists()
    save("Home")
    assert read_meta(meta_path(ap.ssid, ap.bssid))["nickname"] == "Home"
    router.update.assert_called_with(essid="updated")
    FocusViewV2.action_set_nickname(host)
    modal, save = host.app.push_screen.call_args.args
    assert modal._nickname == "Home"
    save("")
    assert "nickname" not in read_meta(meta_path(ap.ssid, ap.bssid))


@pytest.mark.parametrize("ssid", ["Network", None])
def test_router_name_includes_nickname(tmp_path, monkeypatch, ssid):
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path))
    ap = build_ap()
    ap.ssid = ssid
    update_meta("Previous SSID", ap.bssid, nickname="Home")
    host = SimpleNamespace(
        app=SimpleNamespace(target_ap=ap, guessid=lambda ap: None),
        _nickname_key=None, _nickname="", _beacon_samples=deque(),
        _is_probing=lambda: False, _any_campaign_active=lambda: False,
    )
    values = FocusViewV2._router_values(host)
    assert values["essid"] == f"Home ({ssid or '‹hidden›'})"
