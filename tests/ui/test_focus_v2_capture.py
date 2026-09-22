"""Logic-only tests for FocusViewV2 state derivations."""

from wifit3.persist.vault import Vault
from tests.wlan.mocks import build_ap, mock_array
from wifit3.ui import focus_model as fm
from wifit3.campaigns.pmkid import PmkidHarvestAttack
from wifit3.campaigns.deauth import DeauthCampaign
from wifit3.campaigns.eviltwin import EvilTwinCampaign
from wifit3.campaigns.wep import WepCampaign
from wifit3.campaigns.pin import WpsCampaign


def test_recovered_wps_psk_shows_in_status():
    """After a WPS PBC/PIN win the recovered PSK lives on the AP; the status headline
    shows a terminal banner instead of decaying back to 'Listening'."""
    ap = build_ap()
    array = mock_array(cards=1)
    ap.wps_pbc_psk = "hunter2"  # as set by a successful PBC capture

    # We pass None for vault as it's unused in this branch of status_headlines
    lines = fm.status_headlines(ap, array, vault=Vault())
    status_text = "".join(lines)
    assert "PSK recovered" in status_text


def test_pmf_required_disables_deauth():
    """A PMF-Required AP refuses unauthenticated deauth."""
    ap = build_ap()
    ap.pmf_required = True

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
