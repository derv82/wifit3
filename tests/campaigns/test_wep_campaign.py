"""Tests for the WEP Generate IVs campaign orchestrator."""
import asyncio

import pytest

from wifit3.campaigns.campaign import Campaign
from wifit3.dot11 import str_to_mac
from wifit3.models import AccessPoint
from wifit3.wlan.lease import Lease
from wifit3.wlan.wep_store import WepCaptureStore
from wifit3.campaigns.wep import WepCampaign


def _real_lease(mock):
    """Give a MagicMock array a real ``lease()`` (it doubles as its own radio)."""
    mock.lease = lambda **kw: Lease(mock, kw.get("iface") or mock, channel=kw.get("channel"),
                                    fake_mac=kw.get("fake_mac"), bssid=kw.get("bssid"),
                                    ack_tally=kw.get("ack_tally", False))


@pytest.fixture(autouse=True)
def _reset_active():
    """The radio mutex is a Campaign class var: reset it around each test."""
    Campaign.active = None
    yield
    Campaign.active = None


async def test_campaign_starts_and_stops_both_subattacks(mocker):
    iface = mocker.MagicMock()
    iface.send_raw = mocker.AsyncMock(return_value=True)
    iface.send_no_wait = mocker.AsyncMock(return_value=True)
    iface.set_channel = mocker.AsyncMock(return_value=True)
    iface.set_fake_mac = mocker.AsyncMock(return_value=None)   # NONE-card path: random MAC, no AM
    iface.clear_fake_mac = mocker.AsyncMock()
    iface.select_iface.return_value = iface                    # campaign's self.iface is this mock
    iface.current_channel = 6
    _real_lease(iface)
    iface.wep_store = WepCaptureStore()
    ap = AccessPoint(bssid="11:22:33:44:55:66", ssid="W", channel=6, encryption="WEP")

    campaign = WepCampaign(iface, ap)
    campaign.run()
    await asyncio.sleep(0.05)            # let _loop start the daemons
    assert campaign.is_active
    assert campaign.fake_auth.is_active
    assert campaign.replay.is_active

    await campaign.stop()               # cooperative stop + await teardown
    assert not campaign.is_active
    assert not campaign.fake_auth.is_active
    assert not campaign.replay.is_active


def _tracking_iface(mocker, armed):
    """A radio+array mock whose own-MAC register/unregister calls are recorded, with a real lease."""
    iface = mocker.MagicMock()
    iface.send_no_wait = mocker.AsyncMock(return_value=True)
    iface.set_channel = mocker.AsyncMock(return_value=True)
    iface.set_fake_mac = mocker.AsyncMock(return_value=armed)   # armed MAC (SPOOFABLE) or None
    iface.clear_fake_mac = mocker.AsyncMock()
    iface.select_iface.return_value = iface
    iface.current_channel = 6
    iface.wep_store = WepCaptureStore()
    iface._reg, iface._unreg = [], []
    iface.register_own_mac = lambda m: (iface._reg.append(m), m)[1]
    iface.unregister_own_mac = lambda m: iface._unreg.append(m)
    _real_lease(iface)
    return iface


async def test_campaign_arms_and_releases_sta_mac_via_lease(mocker):
    """SPOOFABLE card: the held lease arms + own-registers the STA MAC; the campaign adopts
    lease.mac and shares it to fake-auth/replay; stop() releases (unregisters) it, no fallback."""
    armed = "02:aa:bb:cc:dd:ee"
    iface = _tracking_iface(mocker, armed)
    ap = AccessPoint(bssid="11:22:33:44:55:66", ssid="W", channel=6, encryption="WEP")

    campaign = WepCampaign(iface, ap)
    campaign.run()
    await asyncio.sleep(0.05)
    assert iface._reg == [armed]                                # own-registered the armed MAC
    assert campaign.fake_auth.source_mac == str_to_mac(armed)   # shared to the sub-attacks
    assert campaign.replay.source_mac == str_to_mac(armed)
    assert campaign._own_fallback is None                       # armed path: no manual fallback

    await campaign.stop()
    assert iface._unreg == [armed]                              # lease released it
    iface.clear_fake_mac.assert_awaited()                       # active monitor exited


async def test_campaign_registers_random_sta_on_non_am_card(mocker):
    """A card that can't active-monitor: the lease registers nothing, so the campaign picks a
    random STA and own-registers it itself (the IV guard), releasing it on stop()."""
    iface = _tracking_iface(mocker, None)                       # set_fake_mac -> None
    ap = AccessPoint(bssid="11:22:33:44:55:66", ssid="W", channel=6, encryption="WEP")

    campaign = WepCampaign(iface, ap)
    campaign.run()
    await asyncio.sleep(0.05)
    assert len(iface._reg) == 1                                 # only the campaign's fallback
    assert iface._reg[0] == campaign.fake_auth.source_mac == campaign._own_fallback

    await campaign.stop()
    assert iface._unreg == [iface._reg[0]]                      # fallback unregistered on stop
    assert campaign._own_fallback is None                      # cleared after release


@pytest.mark.slow
async def test_campaign_recovers_key_from_collected_samples(mocker):
    """End-to-end: seed the collector with synthetic crack samples under a
    known key, run the campaign's crack loop, and confirm it recovers it."""
    import asyncio
    import random
    from wifit3.crack.wep import rc4_keystream, ARP_REQUEST_PLAINTEXT

    iface = mocker.MagicMock()
    iface.send_raw = mocker.AsyncMock(return_value=True)
    iface.send_no_wait = mocker.AsyncMock(return_value=True)
    iface.set_channel = mocker.AsyncMock(return_value=True)
    iface.current_channel = 6
    iface.wep_store = WepCaptureStore()
    ap = AccessPoint(bssid="11:22:33:44:55:66", ssid="W", channel=6, encryption="WEP")

    key = bytes.fromhex("6162636465")   # "abcde"
    rng = random.Random(5)
    for _ in range(40_000):
        iv = bytes(rng.randrange(256) for _ in range(3))
        ks = rc4_keystream(iv + key, 16)
        cipher = bytes(ks[i] ^ ARP_REQUEST_PLAINTEXT[i] for i in range(16))
        iface.wep_store.record_crack_sample(ap.bssid, iv, cipher)

    campaign = WepCampaign(iface, ap)
    # Drive the crack loop directly (no real fake-auth/replay needed here).
    campaign._active = True
    samples = iface.wep_store.crack_samples(ap.bssid)
    for iv, cipher in samples:
        from wifit3.crack.wep import keystream_from_arp_cipher
        campaign.cracker.feed(iv, keystream_from_arp_cipher(cipher))
    key_out = await asyncio.get_event_loop().run_in_executor(None, campaign.cracker.recover)
    assert key_out == key


async def test_replay_authenticates_lazily_via_fake_auth(mocker):
    """The campaign wires replay's ensure_associated to fake-auth: replay only
    transmits once fake-auth reports associated (fast path), and an inactive
    fake-auth reports not-associated."""
    iface = mocker.MagicMock()
    iface.wep_store = WepCaptureStore()
    ap = AccessPoint(bssid="11:22:33:44:55:66", ssid="W", channel=6, encryption="WEP")
    campaign = WepCampaign(iface, ap)

    campaign.fake_auth._active = True
    campaign.fake_auth.state = "associated"
    assert await campaign.replay._ensure_associated() is True   # wired + fast path

    campaign.fake_auth._active = False
    assert await campaign.replay._ensure_associated() is False
