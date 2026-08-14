"""CSA deauth campaign: the _loop injects the rewritten beacon, retunes, honors Stop."""
from types import SimpleNamespace

import pytest

from wifit3.campaigns.csa_deauth import CsaDeauthAttack, csa_target_channel
from wifit3.dot11.csa import build_csa_beacon
from wifit3.dot11.ie import ssid_ie, ds_param_ie

_BODY = bytes(range(36))                       # stand-in 24B header + 12B fixed beacon body
_BEACON = _BODY + ssid_ie("Net") + ds_param_ie(6)


def _target(*, beacon=_BEACON, channel=6):
    return SimpleNamespace(bssid="aa:bb:cc:dd:ee:01", channel=channel, ssid="Net",
                           akms=["PSK"], wpa3=False, last_beacon_frame=beacon)


class _FakeArray:
    """Doubles as the WlanArray and the elected interface. Records injected frames and
    trips the campaign's stop flag after ``stop_after`` sends so ``_loop`` exits fast."""

    def __init__(self, *, stop_after=3, start_channel=1):
        self.current_channel = start_channel
        self.sent: list = []
        self.channels: list = []
        self._stop_after = stop_after
        self.campaign = None

    def select_iface(self, channel):
        return self

    def register_forged_mac(self, mac):
        pass

    async def set_channel(self, ch):
        self.current_channel = ch
        self.channels.append(ch)

    async def send_no_wait(self, frame: bytes) -> bool:
        self.sent.append(bytes(frame))
        if self.campaign is not None and len(self.sent) >= self._stop_after:
            self.campaign.stopped = True
        return True


async def test_loop_injects_the_rewritten_beacon_and_counts():
    array = _FakeArray(stop_after=3)
    a = CsaDeauthAttack(array, _target())
    array.campaign = a
    await a._loop()
    expected = build_csa_beacon(_BEACON, a.target_channel)
    assert array.sent and all(frame == expected for frame in array.sent)
    assert a.stats.beacons_sent == len(array.sent)


def test_target_channel_avoids_the_aps_own():
    assert csa_target_channel(6) == 1
    assert csa_target_channel(11) == 1
    assert csa_target_channel(1) == 6
    assert csa_target_channel(149) == 36     # 5GHz stays in-band
    assert csa_target_channel(36) == 40


async def test_loop_retunes_to_the_target_channel():
    array = _FakeArray(stop_after=1, start_channel=1)
    a = CsaDeauthAttack(array, _target(channel=6))
    array.campaign = a
    await a._loop()
    assert array.channels == [6]


async def test_loop_stops_before_injecting_when_already_stopped():
    array = _FakeArray(stop_after=99)
    a = CsaDeauthAttack(array, _target())
    a.stopped = True
    await a._loop()
    assert array.sent == []


def test_requires_a_cached_beacon_to_construct():
    with pytest.raises(ValueError):
        CsaDeauthAttack(_FakeArray(), _target(beacon=None))


def test_visibility_and_reason():
    assert CsaDeauthAttack.visible(_target()) is True
    assert CsaDeauthAttack.visible(SimpleNamespace(akms=[], wpa3=False)) is False
    assert CsaDeauthAttack.ineligible_reason(_target(beacon=None)) == "no beacon captured yet"
    assert CsaDeauthAttack.ineligible_reason(_target()) is None
