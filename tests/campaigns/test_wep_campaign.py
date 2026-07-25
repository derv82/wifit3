"""Tests for the WEP Generate IVs campaign orchestrator."""
import asyncio

import pytest

from wifit3.campaigns.campaign import Campaign
from wifit3.models import AccessPoint
from wifit3.wlan.wep_store import WepCaptureStore
from wifit3.campaigns.wep import WepCampaign


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
