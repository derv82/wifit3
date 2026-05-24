"""Tests for the WEP Generate IVs campaign orchestrator."""
from wifit3.engine.models import AccessPoint
from wifit3.wlan.wep_iv import WepIvCollector
from wifit3.engine.attacks.wep.campaign import WepCampaign


async def test_campaign_starts_and_stops_both_subattacks(mocker):
    iface = mocker.MagicMock()
    iface.send_raw = mocker.AsyncMock(return_value=True)
    iface.set_channel = mocker.AsyncMock(return_value=True)
    iface.current_channel = 6
    iface.wep_collector = WepIvCollector()
    ap = AccessPoint(bssid="11:22:33:44:55:66", ssid="W", channel=6, encryption="WEP")

    campaign = WepCampaign(iface, ap)
    campaign.start()
    assert campaign.is_active
    assert campaign.fake_auth.is_active
    assert campaign.replay.is_active

    campaign.stop()
    assert not campaign.is_active
    assert not campaign.fake_auth.is_active
    assert not campaign.replay.is_active


def test_replay_gated_on_association(mocker):
    """The campaign wires replay's can_inject to fake-auth being associated."""
    iface = mocker.MagicMock()
    iface.wep_collector = WepIvCollector()
    ap = AccessPoint(bssid="11:22:33:44:55:66", ssid="W", channel=6, encryption="WEP")
    campaign = WepCampaign(iface, ap)

    campaign.fake_auth.state = "authenticating"
    assert campaign.replay._can_inject() is False
    campaign.fake_auth.state = "associated"
    assert campaign.replay._can_inject() is True
