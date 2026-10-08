from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from textual.app import App
from textual.widgets import Button, Input, Label, TextArea

from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.decloak import DecloakCampaign
from wifit3.models import AccessPoint
from wifit3.ui.screens.decloak_modal import (
    DecloakCandidateModal, expand_candidates, wordlist_ssids,
)
from wifit3.ui.screens.focus_v2.screen import FocusViewV2
from wifit3.ui.path_picker import PathPickerModal


class _ModalHost(App):
    def __init__(self, modal: DecloakCandidateModal) -> None:
        super().__init__()
        self._modal = modal
        self.result = "unset"

    def on_mount(self) -> None:
        self.push_screen(self._modal, lambda result: setattr(self, "result", result))


@pytest.fixture(autouse=True)
def _reset_campaign():
    Campaign.active = None
    yield
    Campaign.active = None


def test_expand_skips_blank_lines_without_changing_ssid_whitespace():
    assert expand_candidates("  Foo  \n\n  Bar\n", "") == ["  Foo  ", "  Bar"]


def test_expand_substitutes_ssid_token_with_base():
    out = expand_candidates("$ssid\n$ssid Guest\n$ssid-IoT", "Home")
    assert out == ["Home", "Home Guest", "Home-IoT"]


def test_expand_preserves_whitespace_in_template_base():
    out = expand_candidates("$ssid\n$ssid-Guest", " Home ")
    assert out == [" Home ", " Home -Guest"]


def test_expand_requires_a_base_for_templates():
    with pytest.raises(ValueError, match="requires a base"):
        expand_candidates("$ssid\n$ssid Guest\nLiteral", "")


def test_expand_dedupes_preserving_order():
    assert expand_candidates("A\nB\nA\nb\nB", "") == ["A", "B", "b"]


def test_expand_drops_names_over_32_octets():
    short, long = "X" * 32, "Y" * 33
    assert expand_candidates(f"{short}\n{long}", "") == [short]


def test_expand_applies_octet_limit_without_trimming():
    valid = " " + "X" * 30 + " "
    invalid = " " + "X" * 31 + " "
    assert expand_candidates(f"{valid}\n{invalid}", "") == [valid]


def test_wordlist_skips_blanks_and_comments():
    raw = "# common SSIDs\nlinksys\n\n   \n  dlink  \n  # note\nNETGEAR"
    assert wordlist_ssids(raw) == ["linksys", "   ", "  dlink  ", "NETGEAR"]


def test_focus_start_opens_the_candidate_modal():
    hidden = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6)
    app = SimpleNamespace(array=SimpleNamespace(access_points={}), push_screen=MagicMock())
    screen = SimpleNamespace(
        _target_ap=hidden, app=app, _on_decloak_candidates=MagicMock(), _log=MagicMock(),
    )

    FocusViewV2._start_decloak(screen)

    modal, callback = app.push_screen.call_args.args
    assert isinstance(modal, DecloakCandidateModal)
    assert callback is screen._on_decloak_candidates


def test_focus_callback_starts_the_campaign_with_confirmed_candidates():
    hidden = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6)
    controls = MagicMock()
    controls.start.return_value = object()
    screen = SimpleNamespace(
        _target_ap=hidden,
        app=SimpleNamespace(array=object()),
        _controls=controls,
        _log=MagicMock(),
        refresh_buttons=MagicMock(),
    )

    FocusViewV2._on_decloak_candidates(screen, ["Home", "Home Guest"])

    assert controls.start.call_args.args[:3] == (DecloakCampaign, screen.app.array, hidden)
    assert controls.start.call_args.kwargs["candidates"] == ["Home", "Home Guest"]


def test_decloak_hotkey_is_wired_to_the_focus_toggle():
    assert "decloak" in FocusViewV2()._campaign_toggles


async def test_modal_expands_templates_and_requires_review_before_sending():
    modal = DecloakCandidateModal("aa:bb:cc:dd:ee:ff", "Home", ["Home", "Home-Guest"])
    app = _ModalHost(modal)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0)
        area = modal.query_one("#candidates", TextArea)
        area.text = "Home\n$ssid IoT"
        modal.query_one("#btn-decloak", Button).press()
        await pilot.pause(0)
        assert app.screen is modal
        assert area.text == "Home\nHome IoT"
        assert "Confirm" in str(modal.query_one("#btn-decloak", Button).label)
        modal.query_one("#btn-decloak", Button).press()
        await pilot.pause(0)
    assert app.result == ["Home", "Home IoT"]


async def test_modal_preserves_spaces_in_typed_template_base():
    modal = DecloakCandidateModal("aa:bb:cc:dd:ee:ff", "", [])
    app = _ModalHost(modal)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0)
        modal.query_one("#base", Input).value = " Home "
        modal.query_one("#candidates", TextArea).text = "$ssid"
        modal.query_one("#btn-decloak", Button).press()
        await pilot.pause(0)
        assert modal.query_one("#candidates", TextArea).text == " Home "
        modal.query_one("#btn-decloak", Button).press()
        await pilot.pause(0)
    assert app.result == [" Home "]


async def test_modal_normalizes_hidden_candidates_before_confirmation():
    modal = DecloakCandidateModal("aa:bb:cc:dd:ee:ff", "", [])
    app = _ModalHost(modal)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0)
        area = modal.query_one("#candidates", TextArea)
        area.text = f"Home\nHome\n{'X' * 33}"
        modal.query_one("#btn-decloak", Button).press()
        await pilot.pause(0)
        assert app.screen is modal
        assert area.text == "Home"
        modal.query_one("#btn-decloak", Button).press()
        await pilot.pause(0)
    assert app.result == ["Home"]


async def test_modal_refuses_a_template_without_a_base():
    modal = DecloakCandidateModal("aa:bb:cc:dd:ee:ff", "", ["$ssid Guest"])
    app = _ModalHost(modal)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0)
        await pilot.click("#btn-decloak")
        await pilot.pause(0)
        assert app.screen is modal
        assert "requires a base" in modal.query_one("#warn", Label).render().plain
    assert app.result == "unset"


async def test_modal_refuses_an_empty_list_and_stays_open():
    modal = DecloakCandidateModal("aa:bb:cc:dd:ee:ff", "", [])
    app = _ModalHost(modal)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0)
        await pilot.click("#btn-decloak")
        await pilot.pause(0)
        assert app.screen is modal
        assert modal.query_one("#warn", Label).display is True
    assert app.result == "unset"


async def test_wordlist_load_appends_visible_ssids(tmp_path):
    wordlist = tmp_path / "ssids.txt"
    wordlist.write_text("# comment\nOne\nTwo\n", encoding="utf-8")
    modal = DecloakCandidateModal("aa:bb:cc:dd:ee:ff", "", ["Existing"])
    app = _ModalHost(modal)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0)
        await modal._append_wordlist(wordlist)
        assert modal.query_one("#candidates", TextArea).text == "Existing\nOne\nTwo"


async def test_wordlist_button_opens_the_file_picker():
    modal = DecloakCandidateModal("aa:bb:cc:dd:ee:ff", "", [])
    app = _ModalHost(modal)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause(0)
        modal.query_one("#btn-load", Button).press()
        await pilot.pause(0)
        assert isinstance(app.screen, PathPickerModal)
