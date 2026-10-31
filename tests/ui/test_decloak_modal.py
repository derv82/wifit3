"""The decloak modal's candidate list, its live run estimate, and the flags it hands the campaign."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from textual.app import App
from textual.widgets import Checkbox, Input, Label, TextArea

from wifit3.campaigns.decloak import DecloakCampaign, DecloakInput
from wifit3.models import AccessPoint
from wifit3.persist.config import Config
from wifit3.wlan.array import WlanArray
from wifit3.ui.screens.focus_v2.decloak_modal import (
    DecloakInputModal,
    candidates_from_base,
    line_count,
    sendable_ssids,
    seconds_per_ssid,
    wordlist_head,
)

_BSSID = "aa:bb:cc:dd:ee:ff"


# ----- the candidate list ------------------------------------------------------


def test_blank_lines_are_skipped_but_spacing_is_kept():
    # An AP matches an SSID byte for byte, so " Net" must survive as its own candidate.
    assert sendable_ssids("\n  \nNet\n Net\n".splitlines()) == ["Net", " Net"]


def test_duplicates_collapse_preserving_order():
    assert sendable_ssids(["B", "A", "B"]) == ["B", "A"]


def test_names_over_32_octets_are_dropped():
    assert sendable_ssids(["X" * 33, "ok"]) == ["ok"]
    assert sendable_ssids(["é" * 17]) == []                # 34 octets, 17 characters


def test_guesses_lead_with_the_base_then_its_suffixes():
    guesses = candidates_from_base("Loud", "")
    assert guesses[0] == "Loud"
    assert "Loud-Guest" in guesses


def test_a_remembered_ssid_is_offered_first():
    guesses = candidates_from_base("Loud", "Loud-Guest")
    assert guesses[0] == "Loud-Guest"
    assert guesses.count("Loud-Guest") == 1


def test_a_remembered_ssid_is_the_whole_list_without_a_base():
    assert candidates_from_base("", "Songsong") == ["Songsong"]


# ----- what the campaign does with the modal's output --------------------------


def _campaign(chosen):
    array = MagicMock()
    array.access_points = {}
    return DecloakCampaign(array, AccessPoint(bssid=_BSSID, channel=6), chosen)


def test_methods_carry_through_to_the_campaign():
    campaign = _campaign(DecloakInput(["A"], use_directed_probe=False,
                                      use_broadcast_probe=True, use_authassoc_probe=True))

    assert not campaign.use_directed_probe
    assert campaign.use_broadcast_probe and campaign.use_authassoc_probe


def test_planned_counts_every_enabled_method_over_every_ssid():
    campaign = _campaign(DecloakInput(["A", "B"], use_broadcast_probe=True,
                                      use_authassoc_probe=True))
    assert campaign.planned == 6

    campaign.use_broadcast_probe = False
    campaign.use_authassoc_probe = False
    assert campaign.planned == 2


def test_an_explicit_empty_list_does_not_fall_back_to_sibling_guesses():
    sibling = AccessPoint(bssid="aa:bb:cc:dd:ee:f0", channel=6)
    sibling.ssid, sibling.beacons = "Loud", 9
    array = MagicMock()
    array.access_points = {sibling.bssid: sibling}
    hidden = AccessPoint(bssid=_BSSID, channel=6)
    hidden.siblings.append(sibling.bssid)

    campaign = DecloakCampaign(array, hidden, DecloakInput([]))

    assert campaign.candidate_ssids == []
    assert campaign.planned == 0


def test_no_input_falls_back_to_sibling_guesses():
    sibling = AccessPoint(bssid="aa:bb:cc:dd:ee:f0", channel=6)
    sibling.ssid = "Loud"
    array = WlanArray()
    array.access_points[sibling.bssid] = sibling
    hidden = AccessPoint(bssid=_BSSID, channel=6)
    hidden.siblings.append(sibling.bssid)

    campaign = DecloakCampaign(array, hidden)

    assert campaign.candidate_ssids[0] == "Loud"
    assert campaign.use_directed_probe
    assert not campaign.use_broadcast_probe


# ----- the modal on screen -----------------------------------------------------


def _status(modal: DecloakInputModal) -> str:
    return str(modal.query_one("#status", Label).content)


def _text(modal: DecloakInputModal) -> str:
    return modal.query_one("#candidates", TextArea).text


def _wordlist(tmp_path, names: int):
    path = tmp_path / "huge.txt"
    path.write_text("\n".join(f"n{i}" for i in range(names)), encoding="utf-8")
    return str(path)


async def test_the_list_opens_on_the_base_ssid_and_its_suffixes():
    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "Beachball")
        app.push_screen(modal, lambda result: None)
        await pilot.pause(0)

        assert _text(modal).startswith("Beachball\n")
        assert "Beachball-Guest" in _text(modal)


async def test_one_press_sends_the_list_the_status_line_priced():
    app, got = App(), []
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "")
        app.push_screen(modal, got.append)
        await pilot.pause(0)
        modal.query_one("#candidates", TextArea).load_text("Net\nNet-Guest")
        modal.query_one("#use-broadcast", Checkbox).value = True
        await pilot.pause(0)

        assert "2 SSIDs, 4 requests" in _status(modal)

        modal._accept()
        await pilot.pause(0)

    assert got[0] == DecloakInput(["Net", "Net-Guest"], use_directed_probe=True,
                                  use_broadcast_probe=True)


async def test_the_status_line_counts_lines_that_will_not_be_sent():
    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "")
        app.push_screen(modal, lambda result: None)
        await pilot.pause(0)
        modal.query_one("#candidates", TextArea).load_text("Net\nNet\n" + "X" * 33)
        await pilot.pause(0)

        assert "1 SSIDs" in _status(modal) and "(2 dropped)" in _status(modal)


async def test_editing_the_base_always_re_guesses_the_list():
    """No dirty bit: a hand-edited list is replaced, so the base box never looks broken."""
    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "Beachball 2.4")
        app.push_screen(modal, lambda result: None)
        await pilot.pause(0)
        modal.query_one("#candidates", TextArea).load_text("hand-written")
        await pilot.pause(0)

        modal.query_one("#base-ssid", Input).value = "Beachball"
        await pilot.pause(0)

        assert "hand-written" not in _text(modal)
        assert "Beachball-Guest" in _text(modal)
        assert "Beachball 2.4-Guest" not in _text(modal)


async def test_no_method_selected_is_refused():
    app, got = App(), []
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "Alpha")
        app.push_screen(modal, got.append)
        await pilot.pause(0)
        modal.query_one("#use-directed", Checkbox).value = False
        await pilot.pause(0)

        assert _status(modal) == "Pick at least one method"

        modal._accept()
        await pilot.pause(0)
        assert got == []


async def test_an_empty_list_is_refused():
    app, got = App(), []
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "")
        app.push_screen(modal, got.append)
        await pilot.pause(0)

        assert _status(modal) == "No candidate SSIDs to send"

        modal._accept()
        await pilot.pause(0)
        assert got == []


# ----- a wordlist replaces the typed list, under a truncation ------------------


async def test_a_picked_wordlist_takes_over_and_hides_the_list(tmp_path):
    app, got = App(), []
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "Loud")
        app.push_screen(modal, got.append)
        await pilot.pause(0)
        modal.query_one("#wordlist", Input).value = _wordlist(tmp_path, 10)
        await pilot.pause(0)

        assert not modal.query_one("#candidates", TextArea).display
        assert "10 SSIDs, 10 requests" in _status(modal)

        modal._accept()
        await pilot.pause(0)

    assert got[0].candidate_ssids[:2] == ["n0", "n1"]
    assert len(got[0].candidate_ssids) == 10


async def test_clearing_the_path_brings_the_base_list_back_untouched(tmp_path):
    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "Loud")
        app.push_screen(modal, lambda result: None)
        await pilot.pause(0)
        modal.query_one("#wordlist", Input).value = _wordlist(tmp_path, 10)
        await pilot.pause(0)

        modal.query_one("#wordlist", Input).value = ""
        await pilot.pause(0)

        assert modal.query_one("#base-ssid", Input).value == "Loud"
        assert "Loud-Guest" in _text(modal)
        assert modal.query_one("#candidates", TextArea).display


async def test_typing_a_base_takes_the_run_back_from_the_file(tmp_path):
    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "")
        app.push_screen(modal, lambda result: None)
        await pilot.pause(0)
        modal.query_one("#wordlist", Input).value = _wordlist(tmp_path, 10)
        await pilot.pause(0)

        modal.query_one("#base-ssid", Input).value = "Quiet"
        await pilot.pause(0)
        await pilot.pause(0)        # the cleared path posts its own Changed to follow

        assert modal.wordlist_lines == 0
        assert modal.query_one("#wordlist", Input).value == ""
        assert modal.query_one("#candidates", TextArea).display
        assert "Quiet-Guest" in _text(modal)


async def test_a_half_typed_path_is_not_an_error(tmp_path):
    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "Loud")
        app.push_screen(modal, lambda result: None)
        await pilot.pause(0)
        modal.query_one("#wordlist", Input).value = str(tmp_path / "not-there-ye")
        await pilot.pause(0)

        assert modal.wordlist_lines == 0
        assert "SSIDs" in _status(modal)


async def test_a_long_wordlist_opens_truncated_and_the_buttons_move_it(tmp_path):
    """24,001 names is past the longest truncation, so the file is never read whole: the
    modal opens on the shortest one and the buttons move it."""
    app, got = App(), []
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "")
        app.push_screen(modal, got.append)
        await pilot.pause(0)
        modal.query_one("#wordlist", Input).value = _wordlist(tmp_path, 24_001)
        await pilot.pause(0)

        # 300s / 0.15s per directed probe
        assert "2,000 SSIDs" in _status(modal) and "truncated to 5 min" in _status(modal)

        modal.truncate_seconds = 1200
        await pilot.pause(0)
        assert "8,000 SSIDs" in _status(modal)

        modal._accept()
        await pilot.pause(0)

    assert len(got[0].candidate_ssids) == 8000


async def test_pressing_the_chosen_truncation_again_lifts_it(tmp_path):
    from wifit3.ui.screens.focus_v2.decloak_modal import _TruncateButton

    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "")
        app.push_screen(modal, lambda result: None)
        await pilot.pause(0)
        modal.query_one("#wordlist", Input).value = _wordlist(tmp_path, 24_001)
        await pilot.pause(0)
        assert modal.truncate_seconds == 300

        next(b for b in modal.query(_TruncateButton) if b.seconds == 300).press()
        await pilot.pause(0)

        assert modal.truncate_seconds is None
        assert "24,001 SSIDs" in _status(modal) and "truncated" not in _status(modal)


async def test_only_truncations_that_would_shorten_the_run_are_offered(tmp_path):
    """The count is set directly to walk the thresholds without paying for four wordlists."""
    from wifit3.ui.screens.focus_v2.decloak_modal import _TruncateButton

    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "")
        app.push_screen(modal, lambda result: None)
        await pilot.pause(0)

        offered, row_shown = {}, {}
        for lines in (1_999, 2_001, 8_001, 24_001):
            modal.wordlist_lines = lines
            await pilot.pause(0)
            offered[lines] = [b.seconds for b in modal.query(_TruncateButton) if b.display]
            row_shown[lines] = modal.query_one("#truncate").display

        assert offered == {1_999: [], 2_001: [300], 8_001: [300, 1200],
                           24_001: [300, 1200, 3600]}
        assert row_shown == {1_999: False, 2_001: True, 8_001: True, 24_001: True}


async def test_only_the_chosen_truncation_button_is_primary(tmp_path):
    from wifit3.ui.screens.focus_v2.decloak_modal import _TruncateButton

    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "")
        app.push_screen(modal, lambda result: None)
        await pilot.pause(0)
        modal.wordlist_lines = 24_001
        modal.truncate_seconds = 1200
        await pilot.pause(0)

        assert {b.seconds: b.variant for b in modal.query(_TruncateButton)} == {
            300: "default", 1200: "primary", 3600: "default"}


async def test_a_remembered_ssid_opens_first_in_the_list(monkeypatch):
    monkeypatch.setattr(Config, "decloaked_ssids", {_BSSID: "Songsong"})
    app = App()
    async with app.run_test() as pilot:
        modal = DecloakInputModal(_BSSID, "Loud")
        app.push_screen(modal, lambda result: None)
        await pilot.pause(0)

        assert _text(modal).startswith("Songsong\nLoud\n")


# ----- wordlist reading --------------------------------------------------------


def test_line_count_counts_a_file_with_no_trailing_newline(tmp_path):
    path = tmp_path / "w.txt"
    path.write_bytes(b"Alpha\nBeta")
    assert line_count(path) == 2


def test_wordlist_head_keeps_spacing_and_skips_undecodable_lines(tmp_path):
    path = tmp_path / "w.txt"
    path.write_bytes(b" Alpha \n\n\xff\xfe\nBeta\r\n")
    assert wordlist_head(path, 9) == [" Alpha ", "Beta"]
    assert wordlist_head(path, 1) == [" Alpha "]
    assert wordlist_head(path, 0) == []


def test_airtime_counts_every_enabled_method():
    assert seconds_per_ssid(DecloakInput([])) == pytest.approx(0.15)
    assert seconds_per_ssid(DecloakInput([], True, True, True)) == pytest.approx(0.6)
    assert seconds_per_ssid(DecloakInput([], False, False, False)) == 0
