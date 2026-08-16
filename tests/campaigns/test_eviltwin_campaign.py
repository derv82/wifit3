"""EvilTwinCampaign: arms the twin, punts the target channel, auto-stops on a crackable handshake,
and tears down. Plus the WlanSink M1-seed hook that lets our injected M1 pair with the client's M2.
"""
import asyncio
import struct
from types import SimpleNamespace

from wifit3.campaigns.campaign import Campaign
from wifit3.campaigns.eviltwin import EvilTwinCampaign, EvilTwinInput, PuntMode, default_punt_mode
from wifit3.dot11.ap import eapol_m1
from wifit3.dot11.eapol import eapol_key, data_header, LLC_SNAP_EAPOL
from wifit3.dot11.ie import ssid_ie, rates_ie, ds_param_ie, GENERIC_RSN_IE
from wifit3.dot11.parser import WlanFrameParser
from wifit3.crack.handshake import crackable_pairs
from wifit3.models import Handshake, HandshakeMessage
from wifit3.wlan.sink import WlanSink

_BSSID = "94:83:c4:8c:3f:78"
_BSSID_B = bytes.fromhex("9483c48c3f78")
_CLIENT = "02:aa:bb:cc:dd:ee"
_CLIENT_B = bytes.fromhex("02aabbccddee")
_BROADCAST = b"\xff" * 6
_SSID = "GL-Test"
_FIXED = struct.pack("<Q", 0) + struct.pack("<H", 100) + b"\x11\x04"   # TSF, interval, cap (ESS+Privacy)
_BEACON = (b"\x80\x00\x00\x00" + _BROADCAST + _BSSID_B + _BSSID_B + b"\x00\x00"
           + _FIXED + ssid_ie(_SSID) + rates_ie() + ds_param_ie(11) + GENERIC_RSN_IE)


class _FakeIface:
    def __init__(self, channel: int = 1):
        self.sent: list[bytes] = []
        self.current_channel = channel
        self.fake_mac_arms = 0
        self.fake_mac_clears = 0

    async def send_no_wait(self, frame: bytes) -> bool:
        self.sent.append(bytes(frame))
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        self.current_channel = channel
        return True

    async def set_fake_mac(self, mac, bssid=None):
        self.fake_mac_arms += 1
        return ":".join(f"{b:02x}" for b in mac)

    async def clear_fake_mac(self) -> None:
        self.fake_mac_clears += 1

    def register_rx_callback(self, cb) -> None:
        pass

    def unregister_rx_callback(self, cb) -> None:
        pass


class _FakeArray:
    def __init__(self):
        self.access_points: dict = {}
        self.seeded_m1: list[bytes] = []
        self.stray_beacons: dict = {}

    def select_iface(self, channel):
        return None

    def record_injected_eapol(self, frame) -> None:
        self.seeded_m1.append(bytes(frame))

    def ignore_stray_beacons(self, bssid, channel) -> None:
        self.stray_beacons[bssid] = channel

    def stop_ignoring_stray_beacons(self, bssid) -> None:
        self.stray_beacons.pop(bssid, None)


def _target():
    return SimpleNamespace(bssid=_BSSID, ssid=_SSID, channel=11,
                           last_beacon_frame=_BEACON, akm_suites=[2])


def _input(twin, punt, mode=PuntMode.BOTH, period=0.5):
    return EvilTwinInput(twin_iface=twin, punt_iface=punt, twin_channel=1,
                         punt_mode=mode, punt_period_sec=period)


def _crackable_hs():
    hs = Handshake(bssid=_BSSID, client_mac=_CLIENT, beacon_frame=_BEACON, akm_offered=[2])
    hs.messages.append(HandshakeMessage(raw=b"", msg_num=1, replay_hex="0000000000000005",
                                        nonce=b"\xaa" * 32, mic=bytes(16), key_data_len=0,
                                        eapol_payload=bytes(120), timestamp=1.0))
    hs.messages.append(HandshakeMessage(raw=b"", msg_num=2, replay_hex="0000000000000005",
                                        nonce=b"\x02" * 32, mic=b"\x11" * 16, key_data_len=0,
                                        eapol_payload=bytes(120), akm=2, timestamp=1.1))
    return hs


def _client_m2(snonce: bytes) -> bytes:
    payload = eapol_key(key_info=0x010A, key_len=0, replay=1, nonce=snonce, key_data=GENERIC_RSN_IE,
                        mic=bytes(range(16)))
    return data_header(to_ds=True, bssid=_BSSID_B, client=_CLIENT_B) + LLC_SNAP_EAPOL + payload


def test_visible_and_ineligible():
    assert EvilTwinCampaign.visible(_target())
    assert EvilTwinCampaign.visible(SimpleNamespace(ssid=None, akm_suites=[2])) is False
    assert EvilTwinCampaign.visible(SimpleNamespace(ssid="x", akm_suites=[])) is False
    no_beacon = SimpleNamespace(bssid=_BSSID, ssid=_SSID, channel=11,
                                last_beacon_frame=None, akm_suites=[2])
    assert EvilTwinCampaign.ineligible_reason(no_beacon) == "no beacon captured yet"
    assert EvilTwinCampaign.ineligible_reason(_target()) is None
    assert EvilTwinCampaign.ineligible_reason(
        _target(), num_ifaces=1) == "Requires 2 or more wireless interfaces"
    assert EvilTwinCampaign.ineligible_reason(_target(), num_ifaces=2) is None


def test_default_punt_mode():
    assert default_punt_mode(SimpleNamespace(pmf_required=True, pmf_capable=True)) is PuntMode.CSA
    assert default_punt_mode(SimpleNamespace(pmf_required=False, pmf_capable=True)) is PuntMode.BOTH
    assert default_punt_mode(SimpleNamespace(pmf_required=False, pmf_capable=False)) is PuntMode.DEAUTH


def test_punt_frames_by_mode():
    array, twin, punt = _FakeArray(), _FakeIface(), _FakeIface(11)
    cases = {PuntMode.CSA: [0x80], PuntMode.DEAUTH: [0xC0],
             PuntMode.BOTH: [0x80, 0xC0], PuntMode.NONE: []}
    for mode, want_subtypes in cases.items():
        camp = EvilTwinCampaign(array, _target(), _input(twin, punt, mode=mode))
        assert [f[0] for f in camp._punt_frames()] == want_subtypes


async def test_arms_punts_and_tears_down():
    array, twin, punt = _FakeArray(), _FakeIface(), _FakeIface(11)
    array.access_points[_BSSID] = SimpleNamespace(handshakes={})   # nothing crackable yet
    camp = EvilTwinCampaign(array, _target(), _input(twin, punt, mode=PuntMode.BOTH))
    task = asyncio.create_task(camp._loop())
    await asyncio.sleep(0.05)
    camp.stopped = True
    await task
    await camp.teardown()
    assert twin.fake_mac_arms >= 1                    # twin armed on the exact BSSID
    assert twin.current_channel == 11                 # restored to the target channel on teardown
    assert twin.sent                                  # twin beaconed
    assert punt.current_channel == 11 and punt.sent   # punt ran on the target channel
    assert twin.fake_mac_clears == 1                  # torn down once
    assert not camp.captured


async def test_stops_when_sink_has_crackable_handshake():
    array, twin, punt = _FakeArray(), _FakeIface(), _FakeIface(11)
    array.access_points[_BSSID] = SimpleNamespace(handshakes={_CLIENT: _crackable_hs()})
    camp = EvilTwinCampaign(array, _target(), _input(twin, punt, mode=PuntMode.BOTH))
    await asyncio.wait_for(camp._loop(), timeout=1.0)   # exits at once: already captured
    await camp.teardown()
    assert camp.captured
    assert twin.fake_mac_arms >= 1                     # twin still stood up
    assert punt.sent == []                             # never punted: capture was already there


def test_record_injected_m1_pairs_with_real_m2():
    sink = WlanSink()
    sink.update(WlanFrameParser.parse_80211_frame(_BEACON, -40), "card0", 11)
    ap = sink.access_points[_BSSID]
    assert ap.akm_suites == [2] and ap.last_beacon_frame == _BEACON

    sink.record_injected_eapol(eapol_m1(_BSSID_B, _CLIENT_B, b"\xaa" * 32, replay=1))
    sink.update(WlanFrameParser.parse_80211_frame(_client_m2(b"\x02" * 32), -40), "card0", 11)

    hs = ap.handshakes[_CLIENT]
    assert {m.msg_num for m in hs.messages} == {1, 2}
    assert crackable_pairs(hs)


async def test_run_drives_loop_and_restores_channel():
    array, twin, punt = _FakeArray(), _FakeIface(), _FakeIface(11)
    array.select_iface = lambda channel: punt        # the base _drive liveness election
    array.access_points[_BSSID] = SimpleNamespace(handshakes={})
    camp = EvilTwinCampaign(array, _target(), _input(twin, punt, mode=PuntMode.CSA))
    try:
        assert camp.run() is True                    # claims the radio, schedules _drive
        await asyncio.sleep(0.05)
        await camp.stop()                            # cooperative stop, awaits teardown
    finally:
        Campaign.active = None
    assert twin.fake_mac_arms >= 1                    # _loop actually ran (not skipped)
    assert twin.current_channel == 11                # teardown restored the twin channel
    assert punt.sent                                 # the CSA punt went out the TX card
