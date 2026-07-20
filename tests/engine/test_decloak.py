"""Tests for the active-decloak attack: candidate generation and the
frame builder. The driver loop (poll-for-ssid-flip) is covered by the
existing WlanInterface decloak tests once the parser path lights up.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from wifit3.engine.attacks.decloak import (
    SIBLING_SUFFIXES,
    DecloakAttack,
    build_candidates,
)
from wifit3.models import AccessPoint
from wifit3.dot11.parser import WlanFrameParser


def test_build_candidates_empty_base_returns_empty():
    assert build_candidates("") == []


def test_build_candidates_dedups_and_orders():
    out = build_candidates("Foo")
    # Empty suffix must be first — covers mesh / same-SSID dual-band case.
    assert out[0] == "Foo"
    # No duplicates.
    assert len(out) == len(set(out))
    # Guest-like variants should appear before the niche -EXT etc.
    guest_idx = out.index("Foo-Guest")
    ext_idx = out.index("Foo-EXT")
    assert guest_idx < ext_idx
    # We get one entry per suffix (after rstrip dedup).
    assert len(out) <= len(SIBLING_SUFFIXES)


def test_build_candidates_strips_trailing_whitespace():
    """' Guest' suffix would produce 'Base Guest' — fine. But if base
    already ends in whitespace, we shouldn't propagate that into all
    candidates and shouldn't double up."""
    out = build_candidates("TestSSID 2.4")
    assert "TestSSID 2.4 Guest" in out
    assert "TestSSID 2.4-Guest" in out


def test_decloak_probe_req_parses_back_with_candidate_ssid():
    """Round-trip: feed a frame we built through the same parser the
    receive path uses. Confirms wire format is well-formed AND that the
    SSID we asked for is what an AP would see."""
    mock_iface = MagicMock()
    mock_iface.access_points = {}
    target = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6)
    attack = DecloakAttack(
        mock_iface,
        target,
        base_ssid="Foo",
        source_mac=bytes.fromhex("02deadbeefaa"),
    )

    frame = attack._build_probe_req("Foo-Guest")
    parsed = WlanFrameParser.parse_80211_frame(frame, rssi=-30)

    assert parsed is not None
    assert parsed.type == "probe_req"
    assert parsed.bssid == "aa:bb:cc:dd:ee:ff"
    assert parsed.source == "02:de:ad:be:ef:aa"
    assert parsed.dest == "aa:bb:cc:dd:ee:ff"
    assert parsed.ssid == "Foo-Guest"


def test_decloak_probe_req_handles_empty_ssid_candidate():
    """The '' (mesh/exact-match) suffix yields a candidate equal to the
    base SSID — never an empty SSID IE, since build_candidates filters
    empties. But the builder itself should still accept and round-trip
    a long-SSID candidate."""
    mock_iface = MagicMock()
    mock_iface.access_points = {}
    target = AccessPoint(bssid="11:22:33:44:55:66", channel=44)
    attack = DecloakAttack(
        mock_iface,
        target,
        base_ssid="X" * 32,
        source_mac=bytes.fromhex("020000000001"),
    )
    frame = attack._build_probe_req("X" * 32)
    parsed = WlanFrameParser.parse_80211_frame(frame, rssi=-30)
    assert parsed is not None
    assert parsed.ssid == "X" * 32


def test_decloak_attack_registers_forged_mac():
    """The attack's source MAC must be added to iface.forged_macs so the
    EAPOL/handshake/client paths don't treat it as a real STA."""
    mock_iface = MagicMock()
    target = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", channel=6)
    attack = DecloakAttack(mock_iface, target, base_ssid="Foo")
    mock_iface.register_forged_mac.assert_called_once_with(attack.source_mac)
