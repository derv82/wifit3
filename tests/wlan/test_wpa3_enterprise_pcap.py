"""Parse one beacon per WPA3-Enterprise RSN profile (tests/fixtures/make_wpa3_enterprise_pcap.py)
and assert the RSN/AKM classification per BSSID."""
import pytest

from tests.fixtures.make_wpa3_enterprise_pcap import beacons
from wifit3.dot11.parser import WlanFrameParser

# BSSID -> (encryption label, wpa3, transition_mode) the parser must produce.
EXPECTED = {
    "02:73:a3:00:00:01": ("WPA2-EAP-CCMP", False, False),
    "02:73:a3:00:00:02": ("WPA3-EAP-SHA256-CCMP", True, False),
    "02:73:a3:00:00:03": ("WPA2/WPA3-EAP+EAP-SHA256-CCMP", True, True),
    "02:73:a3:00:00:04": ("WPA3-EAP-SUITE-B-192-GCMP-256", True, False),
    "02:73:a3:00:00:05": ("WPA2-EAP-SHA256-CCMP", False, False),
    "02:73:a3:00:00:06": ("WPA2-EAP-SUITE-B-192-GCMP-256", False, False),
    "02:73:a3:00:00:07": ("WPA2-EAP-SUITE-B-192-GCMP-256", False, False),
}


@pytest.fixture(scope="module")
def parsed():
    out = {}
    for raw, _ts in beacons():
        packet = WlanFrameParser.parse_80211_frame(raw, -50)
        out[packet.bssid] = packet
    return out


def test_every_profile_parses(parsed):
    assert set(parsed) == set(EXPECTED)


@pytest.mark.parametrize("bssid", list(EXPECTED))
def test_wpa3_enterprise_classification(parsed, bssid):
    packet = parsed[bssid]
    encryption, wpa3, transition = EXPECTED[bssid]
    assert packet.encryption == encryption
    assert packet.wpa3 is wpa3
    assert packet.transition_mode is transition
