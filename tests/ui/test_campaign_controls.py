"""Unit tests for ``CampaignControls``: the start mutex plus the
request_stop / stop / reap lifecycle that Focus drives campaigns through."""
import pytest

from wifit3.campaigns.campaign import Campaign
from wifit3.ui.screens.focus_v2.campaign_controls import CampaignControls


class _StubCampaign(Campaign):
    """A controllable campaign: ``run()`` claims the radio without an asyncio task,
    and ``done`` is a plain flag the test flips."""
    key = "stub"

    def __init__(self, array, ap, log=None):
        super().__init__(ap=ap, array=array)
        self.log = log
        self.finished = False

    def run(self) -> bool:
        if Campaign.active is not None:
            return False
        Campaign.active = self
        self.stopped = False
        return True

    @property
    def done(self) -> bool:
        return self.finished


@pytest.fixture(autouse=True)
def _reset_active():
    Campaign.active = None
    yield
    Campaign.active = None


def test_start_runs_campaign_and_claims_the_radio():
    controls = CampaignControls()
    camp = controls.start(_StubCampaign, None, None)
    assert isinstance(camp, _StubCampaign)
    assert controls.current is camp
    assert Campaign.active is camp          # run() claimed the mutex


def test_start_blocked_while_a_campaign_is_active():
    controls = CampaignControls()
    first = controls.start(_StubCampaign, None, None)
    second = controls.start(_StubCampaign, None, None)
    assert second is None                   # mutex: only one at a time
    assert controls.current is first


def test_start_blocked_when_another_owner_holds_the_radio():
    Campaign.active = _StubCampaign(None, None)   # some other screen owns it
    controls = CampaignControls()
    assert controls.start(_StubCampaign, None, None) is None
    assert controls.current is None


def test_request_stop_keeps_campaign_for_reaping():
    controls = CampaignControls()
    camp = controls.start(_StubCampaign, None, None)
    controls.request_stop()
    assert camp.stopped is True
    assert controls.current is camp         # kept so its result can be reaped
    assert controls.reap() is None          # still running -> nothing yet
    camp.finished = True
    assert controls.reap() is camp          # reaped once done
    assert controls.current is None         # slot freed


def test_stop_forgets_immediately_without_reaping():
    controls = CampaignControls()
    camp = controls.start(_StubCampaign, None, None)
    controls.stop()
    assert camp.stopped is True
    assert controls.current is None         # forgotten at once
    assert controls.reap() is None          # nothing left to reap


def test_reap_returns_none_until_the_campaign_is_done():
    controls = CampaignControls()
    camp = controls.start(_StubCampaign, None, None)
    assert controls.reap() is None
    camp.finished = True
    assert controls.reap() is camp
