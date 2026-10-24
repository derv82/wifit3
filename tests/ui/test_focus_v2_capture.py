"""Logic-only tests for FocusViewV2 state derivations."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from wifit3.persist.vault import Vault
from tests.wlan.mocks import build_ap, mock_array
from wifit3.ui import focus_model as fm
from wifit3.campaigns.pmkid import PmkidHarvestAttack
from wifit3.campaigns.deauth import DeauthCampaign
from wifit3.campaigns.eviltwin import EvilTwinCampaign
from wifit3.campaigns.wep import WepCampaign
from wifit3.campaigns.pin import WpsCampaign
from wifit3.models import AccessPoint
from wifit3.ui.screens.focus_v2.screen import FocusViewV2


def test_recovered_wps_psk_shows_in_status():
    """After a WPS PBC/PIN win the recovered PSK lives on the AP; the status headline
    shows a terminal banner instead of decaying back to 'Listening'."""
    ap = build_ap()
    array = mock_array(cards=1)
    ap.wps_pbc_psk = "hunter2"  # as set by a successful PBC capture

    # We pass None for vault as it's unused in this branch of status_headlines
    lines = fm.status_headlines(ap, array, vault=Vault())
    status_text = "".join(lines)
    assert "PSK: hunter2" in status_text


def test_finish_wps_preserves_empty_pin():
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="TestNet")
    vault = Vault()
    host = SimpleNamespace(app=SimpleNamespace(vault=vault), _log=MagicMock())
    camp = SimpleNamespace(
        target=ap,
        bssid=ap.bssid,
        state=SimpleNamespace(found_pin="", found_psk="hunter2"),
    )

    FocusViewV2._finish_wps(host, camp)

    assert ap.wps_pin == ""
    assert vault.persisted(ap.bssid)[0].pin == ""


def test_pmf_required_disables_deauth():
    """A PMF-Required AP refuses unauthenticated deauth."""
    ap = build_ap()
    ap.pmf_required = True
    ap.pmf_capable = True            # a valid PMF-Required AP is MFPC=1 and MFPR=1

    # DeauthCampaign block reason should be non-empty (truthy) when blocked
    blocked = fm.deauth_blocked(ap)
    assert blocked is True, "PMF Required AP should block deauth"


def test_campaign_visibility_for_wpa2():
    """The attack buttons are encryption-conditional. For a WPA2 AP (no WPS, not WPA3): 
    PMKID + Deauth + EvilTwin apply. The rest hide."""
    ap = build_ap(encryption="WPA2")
    ap.wpa3 = False
    ap.wps = False

    # These should be visible
    assert PmkidHarvestAttack.visible(ap) is True
    assert DeauthCampaign.visible(ap) is True
    assert EvilTwinCampaign.visible(ap) is True

    # These should be hidden
    assert WepCampaign.visible(ap) is False
    assert WpsCampaign.visible(ap) is False


def test_save_line_joins_natively_instead_of_gluing_a_slash(monkeypatch):
    """The line used to be f'{captures_dir}/{name}', which mixed separators once the
    save directory was absolute."""
    import os
    from pathlib import Path
    from types import SimpleNamespace

    from wifit3.persist.config import Config
    from wifit3.ui.screens.focus_v2.screen import _save_line

    result = SimpleNamespace(
        was_new=True,
        path=Path("ignored/AP_30-85-a9-a9-a9-a9_1700000000_pmkid.hc22000"))

    monkeypatch.setattr(Config, "captures_dir", str(Path("/tmp/loot").resolve()))
    line = _save_line(result)
    assert "/" not in line.replace("[dim]", "").replace("[/dim]", "") or os.sep == "/"
    assert str(Path(Config.captures_dir)) in line

    monkeypatch.setattr(Config, "captures_dir", "captures")
    assert f"captures{os.sep}AP" in _save_line(result)
