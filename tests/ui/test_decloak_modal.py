"""The decloak modal's candidate list, its live run estimate, and the flags it hands the campaign."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from textual.app import App
from textual.widgets import Checkbox, Input, Label, TextArea

from wifit3.campaigns.decloak import DecloakCampaign, DecloakInput
from wifit3.models import AccessPoint
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


def _wordlist(tmp_path, names: int):
    path = tmp_path / "huge.txt"
    path.write_text("\n".join(f"n{i}" for i in range(names)), encoding="utf-8")
    return str(path)


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


# ----- a wordlist replaces the typed list, under a truncation ------------------


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
    assert seconds_per_ssid(DecloakInput([], True, True, True)) == pytest.approx(1.3)
    assert seconds_per_ssid(DecloakInput([], False, False, False)) == 0
