"""Deauth campaign: round-robin clients then broadcast each round, stopping only on
a NEW crackable handshake (a pre-existing one must not end the campaign instantly).

No hardware: the fake array vends a recording radio and a non-blocking ``wait_until``.
A crackable capture is modelled as a plain-PSK PMKID (AKM 2), which ``pmkid_crackable``
counts, so the loop's stop condition fires without building a real 4-way.
"""
from types import SimpleNamespace

from wifit3.campaigns.deauth import DeauthCampaign
from wifit3.models import Handshake
from wifit3.wlan.interface import DeauthResult
from wifit3.wlan.lease import Lease

_BSSID = "aa:bb:cc:dd:ee:01"


def _pmkid_hs(client_mac: str) -> Handshake:
    """A crackable capture: plain-PSK PMKID (AKM 2)."""
    return Handshake(bssid=_BSSID, client_mac=client_mac, pmkid=b"\x11" * 16, pmkid_akm=2)


class _FakeRadio:
    """Records deauth_client / deauth_broadcast calls; the lease tunes it (channel-only)."""

    def __init__(self):
        self.current_channel = 6
        self.client_calls: list = []
        self.bcast_calls: list = []
        self.on_client = None       # test hook: fired after each deauth_client
        self.on_bcast = None        # test hook: fired after each deauth_broadcast

    async def set_channel(self, ch, scan=False):
        self.current_channel = ch
        return True

    async def deauth_client(self, ap_bssid, client_bssid, rounds=10):
        self.client_calls.append((ap_bssid, client_bssid, rounds))
        if self.on_client:
            self.on_client()
        return DeauthResult(client_sent=rounds, client_acks=rounds,
                            ap_sent=rounds, ap_acks=rounds, measured=True)

    async def deauth_broadcast(self, ap_bssid, count=20):
        self.bcast_calls.append((ap_bssid, count))
        if self.on_bcast:
            self.on_bcast()
        return count


class _FakeArray:
    """Vends the radio and a non-blocking wait_until; holds the sink dicts the campaign reads."""

    def __init__(self, radio, ap, clients):
        self._radio = radio
        self.access_points = {_BSSID: ap}
        self.clients = clients
        self.forged_macs = set()

    def select_iface(self, channel):
        return self._radio

    def lease(self, channel=None, fake_mac=None, bssid=None, ack_tally=False, iface=None):
        return Lease(self, iface or self._radio, channel=channel, fake_mac=fake_mac,
                     bssid=bssid, ack_tally=ack_tally)

    async def wait_until(self, condition, timeout, poll=0.05):
        return bool(condition())   # no real waiting: evaluate the predicate once


def _target():
    return SimpleNamespace(bssid=_BSSID, channel=6, ssid="TESTNET",
                           pmf_required=False, akm_suites=[2])


def _client(bssid=_BSSID):
    return SimpleNamespace(bssid=bssid)


async def test_round_robins_clients_then_broadcasts_and_stops_on_capture():
    """Two clients get a targeted burst each (in order), then a broadcast; the broadcast
    provokes the handshake, so the campaign stops with captured=True."""
    ap = SimpleNamespace(bssid=_BSSID, handshakes={})
    radio = _FakeRadio()
    array = _FakeArray(radio, ap, {"c1": _client(), "c2": _client()})
    camp = DeauthCampaign(array, _target())
    radio.on_bcast = lambda: ap.handshakes.__setitem__("c1", _pmkid_hs("c1"))

    await camp._loop()

    assert [c[1] for c in radio.client_calls] == ["c1", "c2"]      # round-robin, in order
    assert len(radio.bcast_calls) == 1                             # one broadcast that round
    assert camp.captured is True
    assert camp.client_sent == 40 and camp.client_acks == 40       # 2 clients x 10 rounds x 2 dirs
    assert camp.bcast_sent == 20


async def test_pre_existing_handshake_does_not_stop_instantly():
    """A handshake captured BEFORE the campaign starts is the baseline: the loop still
    runs (deauths its client) rather than exiting at once. It ends here via a stop hook."""
    ap = SimpleNamespace(bssid=_BSSID, handshakes={"old": _pmkid_hs("old")})
    radio = _FakeRadio()
    array = _FakeArray(radio, ap, {"c1": _client()})
    camp = DeauthCampaign(array, _target())
    radio.on_client = lambda: setattr(camp, "stopped", True)       # end after one burst

    await camp._loop()

    assert [c[1] for c in radio.client_calls] == ["c1"]            # it DID deauth, not short-circuit
    assert radio.bcast_calls == []                                 # stopped before the broadcast
    assert camp.captured is False                                  # no NEW capture beyond baseline


async def test_no_clients_broadcasts_each_round():
    """With no known clients the campaign still broadcasts; the broadcast provokes the
    capture and it stops."""
    ap = SimpleNamespace(bssid=_BSSID, handshakes={})
    radio = _FakeRadio()
    array = _FakeArray(radio, ap, {})
    camp = DeauthCampaign(array, _target())
    radio.on_bcast = lambda: ap.handshakes.__setitem__("c1", _pmkid_hs("c1"))

    await camp._loop()

    assert radio.client_calls == []                               # no clients to target
    assert len(radio.bcast_calls) == 1
    assert camp.captured is True


def test_visible_requires_psk_and_no_pmf():
    assert DeauthCampaign.visible(SimpleNamespace(pmf_required=False, akm_suites=[2])) is True
    assert DeauthCampaign.visible(SimpleNamespace(pmf_required=False, akm_suites=[8])) is False   # SAE
    assert DeauthCampaign.visible(SimpleNamespace(pmf_required=False, akm_suites=[])) is False    # WEP/open
    assert DeauthCampaign.visible(SimpleNamespace(pmf_required=True, akm_suites=[2])) is False    # PMF
