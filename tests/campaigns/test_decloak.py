"""Active decloak: candidate generation, the probe frame builder, and the association
sweep's adherence to the 802.11 state machine."""
from __future__ import annotations

from unittest.mock import MagicMock

from wifit3.campaigns.decloak import (
    SIBLING_SUFFIXES,
    DecloakCampaign,
    DecloakInput,
    candidates_from_sibling,
)
from wifit3.campaigns.auth_assoc import AssocState
from wifit3.models import AccessPoint
from wifit3.dot11.parser import WlanFrameParser
from wifit3.dot11.probe import probe_req

_BSSID = "aa:bb:cc:dd:ee:ff"


def _campaign(candidates, *, target=None):
    array = MagicMock()
    array.access_points = {}
    return DecloakCampaign(array, target or AccessPoint(bssid=_BSSID, channel=6),
                           DecloakInput(candidates, use_authassoc_probe=True))


# ----- candidate generation ----------------------------------------------------


def test_no_sibling_ssid_yields_no_candidates():
    assert candidates_from_sibling("") == []


def test_candidates_dedup_and_keep_suffix_order():
    out = candidates_from_sibling("Foo")
    assert out[0] == "Foo"                      # empty suffix: the same-SSID dual-band case
    assert len(out) == len(set(out))
    assert out.index("Foo-Guest") < out.index("Foo-EXT")
    assert len(out) <= len(SIBLING_SUFFIXES)


def test_candidates_strip_trailing_whitespace():
    out = candidates_from_sibling("TestSSID 2.4")
    assert "TestSSID 2.4 Guest" in out
    assert "TestSSID 2.4-Guest" in out


def test_candidates_drop_names_over_32_octets():
    out = candidates_from_sibling("X" * 31)
    assert "X" * 31 in out                      # the bare sibling still fits
    assert all(len(c.encode("utf-8")) <= 32 for c in out)
    assert ("X" * 31) + "-Guest" not in out


# ----- frame building ----------------------------------------------------------


def test_probe_req_round_trips_with_the_candidate_ssid():
    """Feed a frame we built back through the parser the receive path uses: the wire
    format is well formed and the SSID we asked for is what an AP would see."""
    campaign = _campaign(["Foo-Guest"])
    frame = probe_req(campaign.bssid_bytes, campaign.source_mac, "Foo-Guest", channel=6)
    parsed = WlanFrameParser.parse_80211_frame(frame, rssi=-30)

    assert parsed is not None
    assert parsed.type == "probe_req"
    assert parsed.bssid == _BSSID
    assert parsed.dest == _BSSID
    assert parsed.ssid == "Foo-Guest"


def test_probe_req_round_trips_a_full_length_ssid():
    campaign = _campaign(["X" * 32],
                         target=AccessPoint(bssid="11:22:33:44:55:66", channel=44))
    frame = probe_req(campaign.bssid_bytes, campaign.source_mac, "X" * 32, channel=44)
    parsed = WlanFrameParser.parse_80211_frame(frame, rssi=-30)
    assert parsed is not None and parsed.ssid == "X" * 32


async def test_teardown_unregisters_the_forged_mac():
    campaign = _campaign(["Foo"])
    await campaign.teardown()
    campaign.array.unregister_own_mac.assert_called_once_with(campaign.source_mac)


# ----- association sweep -------------------------------------------------------


class _FakeAssociation:
    """Records the auth/assoc calls a sweep makes, and replays scripted verdicts."""

    def __init__(self, verdicts: dict, *, deauth_after: str = ""):
        self._verdicts = verdicts
        self._deauth_after = deauth_after
        self.state = AssocState.AUTHENTICATED   # _claim_each's caller authenticates first
        self.calls: list[str] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    async def authenticate(self) -> bool:
        self.calls.append("auth")
        self.state = AssocState.AUTHENTICATED
        return True

    async def associate_as(self, ssid: str):
        self.calls.append(f"assoc:{ssid}")
        status = self._verdicts.get(ssid)
        if status == 0:
            self.state = AssocState.ASSOCIATED
        elif ssid == self._deauth_after:
            self.state = AssocState.UNAUTHENTICATED     # the AP deauthed us
        return status


async def test_sweep_authenticates_once_for_every_candidate():
    campaign = _campaign(["A", "B", "C"])
    association = _FakeAssociation({"C": 0})
    found = await campaign._claim_each(association)

    assert found == "C"
    assert association.calls == ["assoc:A", "assoc:B", "assoc:C"]   # no re-auth
    campaign.array.decloak.assert_called_once_with(campaign.ap, "C", "assoc")


async def test_sweep_reauthenticates_only_after_a_deauth():
    campaign = _campaign(["A", "B"])
    association = _FakeAssociation({}, deauth_after="A")
    found = await campaign._claim_each(association)

    assert found is None
    assert association.calls == ["assoc:A", "auth", "assoc:B"]


async def test_sweep_stops_when_asked():
    campaign = _campaign(["A", "B"])
    campaign.stopped = True
    association = _FakeAssociation({"B": 0})
    assert await campaign._claim_each(association) is None
    assert association.calls == []
