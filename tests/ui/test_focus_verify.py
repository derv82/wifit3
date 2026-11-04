"""Focus's Verify button: the stored credentials it offers, the campaign it starts, and how it reaps it."""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.verify.campaign import VerifyOutcome
from wifit3.campaigns.verify.pin import VerifyPinCampaign
from wifit3.campaigns.verify.psk import VerifyPskCampaign
from wifit3.campaigns.verify.wep import VerifyWepCampaign
from wifit3.models import AccessPoint, CaptureType, PersistedCapture
from wifit3.persist.ap_state import Key
from wifit3.ui.screens.focus_v2.campaign_controls import CampaignControls
from wifit3.ui.screens.focus_v2.screen import FocusViewV2
from wifit3.ui.screens.focus_v2.verify_menu import key_option_prompts

BSSID = "00:11:22:33:44:55"


@pytest.fixture(autouse=True)
def _reset_active():
    Campaign.active = None
    yield
    Campaign.active = None


def _key(key_type: CaptureType, value: str, saved_at: int) -> Key:
    capture = PersistedCapture(type=key_type, timestamp=saved_at, path=f"/tmp/{saved_at}", bssid=BSSID,
                               value=value, ssid="TestNet")
    return Key(type=key_type, value=value, captures=(capture,), hashline_ids=frozenset(), live_verifications=())


def _host(keys=()):
    vault = MagicMock()
    vault.ap_state.return_value = SimpleNamespace(keys=tuple(keys))
    return SimpleNamespace(app=SimpleNamespace(vault=vault, array=MagicMock()),
                           _target_ap=AccessPoint(bssid=BSSID, ssid="TestNet", channel=6),
                           _controls=MagicMock(), _log=MagicMock(), refresh_buttons=MagicMock(),
                           _verify_key=None)


def test_verify_keys_are_newest_first():
    old_psk, pin, new_psk = (_key(CaptureType.WPA_PSK, "old", 10), _key(CaptureType.WPS_PIN, "12345670", 20),
                             _key(CaptureType.WPA_PSK, "new", 30))
    host = _host([old_psk, pin, new_psk])

    assert FocusViewV2._verify_keys(host, host._target_ap) == [new_psk, pin, old_psk]


@pytest.mark.parametrize("key_type, campaign_class", [
    (CaptureType.WPA_PSK, VerifyPskCampaign),
    (CaptureType.WPS_PIN, VerifyPinCampaign),
    (CaptureType.WEP, VerifyWepCampaign),
])
def test_the_chosen_credentials_type_picks_the_campaign(key_type, campaign_class):
    key = _key(key_type, "6162636465", 10)
    host = _host([key])

    FocusViewV2._start_verify(host, key)

    started_class = host._controls.start.call_args.args[0]
    assert started_class is campaign_class
    assert host._controls.start.call_args.kwargs["verify_value"] == "6162636465"
    assert host._verify_key is key


def test_start_logs_the_credential_being_verified():
    key = _key(CaptureType.WPA_PSK, "hunter22", 10)
    host = _host([key])

    FocusViewV2._start_verify(host, key)

    assert host._log.call_args_list[0].args[0] == "Weryfikuję PSK [cyan]hunter22[/]…"


def test_a_dismissed_menu_starts_nothing():
    host = _host([_key(CaptureType.WPA_PSK, "a", 10), _key(CaptureType.WPA_PSK, "b", 20)])

    FocusViewV2._start_verify(host, None)

    host._controls.start.assert_not_called()


async def test_controls_construct_a_verify_campaign_with_its_value():
    controls = CampaignControls()
    array = MagicMock()
    array.wait_until = AsyncMock(return_value=False)

    campaign = controls.start(VerifyPskCampaign, array, AccessPoint(bssid=BSSID, ssid="TestNet", channel=6),
                              verify_value="hunter22", log=print)
    await campaign.stop()

    assert (campaign.value, campaign.log) == ("hunter22", print)


@pytest.mark.parametrize("outcome, line, correct", [
    (VerifyOutcome.VERIFIED, "[green]Poprawne[/green]", True),
    (VerifyOutcome.REJECTED, "[red]Błędne (nieaktualne)[/red]", False),
])
def test_finish_records_a_verified_or_rejected_key(outcome, line, correct):
    key = _key(CaptureType.WPA_PSK, "hunter22", 10)
    host = _host([key])
    host._verify_key = key
    campaign = SimpleNamespace(stopped=False, outcome=outcome, error=None)

    FocusViewV2._finish_verify(host, campaign)

    assert line in host._log.call_args.args[0]
    host.app.vault.record_live_verification.assert_called_once_with(key, correct=correct)


@pytest.mark.parametrize("campaign", [
    SimpleNamespace(stopped=False, outcome=VerifyOutcome.ERROR, error="AP never sent EAPOL M1"),
    SimpleNamespace(stopped=True, outcome=None, error=None),
])
def test_finish_records_nothing_for_an_error_or_a_stop(campaign):
    host = _host()
    host._verify_key = _key(CaptureType.WPA_PSK, "hunter22", 10)

    FocusViewV2._finish_verify(host, campaign)

    host.app.vault.record_live_verification.assert_not_called()
    if campaign.error:
        assert "[yellow]Błąd:[/yellow] AP never sent EAPOL M1" in host._log.call_args.args[0]


def test_menu_rows_align_the_dates_right():
    this_year = time.localtime().tm_year
    sep_23 = int(time.mktime((this_year, 9, 23, 5, 6, 0, 0, 0, -1)))
    sep_18 = int(time.mktime((this_year, 9, 18, 13, 14, 0, 0, 0, -1)))
    prompts = key_option_prompts([_key(CaptureType.WPA_PSK, "hunter22", sep_23),
                                  _key(CaptureType.WPS_PIN, "12345670", sep_18)])

    assert [prompt.plain for prompt in prompts] == ["PSK: hunter22        Sep 23 05:06",
                                                   "WPS PIN: 12345670    Sep 18 13:14"]
