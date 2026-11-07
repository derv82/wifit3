"""Tests for the Verify campaigns."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from wifit3.campaigns.verify.campaign import VerifyOutcome
from wifit3.campaigns.verify.pin import VerifyPinCampaign
from wifit3.campaigns.verify.psk import VerifyPskCampaign
from wifit3.campaigns.verify.wep import VerifyWepCampaign
from wifit3.campaigns.wps.registrar import AttemptOutcome, PinResult
from wifit3.crack.wep import rc4_keystream
from wifit3.dot11 import str_to_mac
from wifit3.dot11.eapol import HMAC_MD5_RC4, HMAC_SHA1_AES, KeyDescriptorVersion
from wifit3.dot11.packet import DeauthPacket, EapolPacket, WepDataPacket
from wifit3.dot11.parser import WlanFrameParser
from wifit3.dot11.wep.crypto import icv
from wifit3.models import AccessPoint

BSSID = "00:11:22:33:44:55"
OUR_MAC = "00:aa:bb:cc:dd:ee"


@pytest.fixture(autouse=True)
def _no_leaving_echo_wait(monkeypatch):
    monkeypatch.setattr("wifit3.campaigns.verify.campaign._LEAVING_ECHO_S", 0)


class _FakeLease:
    def __init__(self, iface, mac):
        self.iface = iface
        self.mac = mac

    async def __aenter__(self):
        return self.iface

    async def __aexit__(self, *args):
        return False


def _iface(acked: bool = True):
    iface = MagicMock()
    iface.set_channel = AsyncMock()
    iface.send_no_wait = AsyncMock()
    iface.send_until_ack = AsyncMock(return_value=acked)
    return iface


def _array(iface, *, lease_mac=OUR_MAC, heard=True):
    array = MagicMock()
    array.members = [iface]
    array.access_points = {BSSID: AccessPoint(bssid=BSSID, ssid="TestNet", channel=1)}
    array.select_iface.return_value = iface
    array.lease.return_value = _FakeLease(iface, lease_mac)
    array.wait_until = AsyncMock(return_value=heard)
    return array


def _eapol_from_ap(msg_num: int, replay: int,
                   key_descriptor_version: KeyDescriptorVersion = HMAC_SHA1_AES) -> EapolPacket:
    return EapolPacket(type="eapol", type_id=2, subtype_id=0, bssid=BSSID, source=BSSID, dest=OUR_MAC,
                       to_ds=False, from_ds=True, rssi=-50, raw=b"\x00" * 30, msg_num=msg_num,
                       key_info=0x0088 | key_descriptor_version,
                       nonce=b"\x11" * 32, replay_counter=replay.to_bytes(8, "big"))


def _associates(monkeypatch, associated: bool = True):
    monkeypatch.setattr("wifit3.wlan.station.Association.associate", AsyncMock(return_value=associated))


async def _run(campaign_class, array, value):
    campaign = campaign_class(array, array.access_points[BSSID], value, log=MagicMock())
    await campaign._loop()
    return campaign


async def test_verify_runs_on_a_lease_of_the_targets_channel_without_claiming_a_device(monkeypatch):
    iface = _iface()
    array = _array(iface)
    array.next_frame = AsyncMock(side_effect=[_eapol_from_ap(1, 1), _eapol_from_ap(3, 2)])
    _associates(monkeypatch)

    await _run(VerifyPskCampaign, array, "pw")

    array.claim.assert_not_called()
    iface.set_channel.assert_not_called()
    assert array.lease.call_args.kwargs["channel"] == 1
    assert array.lease.call_args.kwargs["iface"] is iface


async def test_verify_psk_m3_is_verified(monkeypatch):
    array = _array(_iface())
    array.next_frame = AsyncMock(side_effect=[_eapol_from_ap(1, 1), _eapol_from_ap(3, 2)])
    _associates(monkeypatch)

    campaign = await _run(VerifyPskCampaign, array, "pw")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.VERIFIED, None)
    steps = [call.args[0] for call in campaign.log.call_args_list]
    assert steps == ["associated", "M1 received, M2 sent", "M3 received"]


async def test_verify_psk_m2_takes_the_m1s_key_descriptor_version(monkeypatch):
    iface = _iface()
    array = _array(iface)
    array.next_frame = AsyncMock(side_effect=[_eapol_from_ap(1, 1, key_descriptor_version=HMAC_MD5_RC4),
                                              _eapol_from_ap(3, 2, key_descriptor_version=HMAC_MD5_RC4)])
    _associates(monkeypatch)

    await _run(VerifyPskCampaign, array, "pw")

    m2 = WlanFrameParser.parse_80211_frame(iface.send_until_ack.await_args.args[0], rssi=0)
    assert m2.key_info == 0x0109


async def test_verify_psk_m3_after_a_resent_m1_is_verified(monkeypatch):
    array = _array(_iface())
    array.next_frame = AsyncMock(side_effect=[_eapol_from_ap(1, 1), _eapol_from_ap(1, 2), _eapol_from_ap(3, 3)])
    _associates(monkeypatch)

    campaign = await _run(VerifyPskCampaign, array, "pw")

    assert campaign.outcome is VerifyOutcome.VERIFIED


async def test_verify_psk_m1_resent_after_each_m2_is_rejected(monkeypatch):
    iface = _iface()
    array = _array(iface)
    array.next_frame = AsyncMock(side_effect=[_eapol_from_ap(1, replay) for replay in (1, 2, 3, 4)])
    _associates(monkeypatch)

    campaign = await _run(VerifyPskCampaign, array, "pw")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.REJECTED, None)
    assert iface.send_until_ack.await_count == 3


def _deauth_from_ap(reason: int) -> DeauthPacket:
    return DeauthPacket(type="deauth", type_id=0, subtype_id=12, bssid=BSSID, source=BSSID, dest=OUR_MAC,
                        to_ds=False, from_ds=False, rssi=-50, raw=b"\x00" * 26, reason=reason)


async def test_verify_psk_deauth_after_a_resent_m1_is_rejected(monkeypatch):
    """hostapd answers its first bad M2s by re-sending M1, then deauths (reason 15) at its retry limit."""
    array = _array(_iface())
    array.next_frame = AsyncMock(side_effect=[_eapol_from_ap(1, 2), _eapol_from_ap(1, 3), _deauth_from_ap(15)])
    _associates(monkeypatch)

    campaign = await _run(VerifyPskCampaign, array, "pw")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.REJECTED, None)


async def test_verify_psk_deauth_after_a_resent_m1_for_another_reason_is_an_error(monkeypatch):
    array = _array(_iface())
    array.next_frame = AsyncMock(side_effect=[_eapol_from_ap(1, 2), _eapol_from_ap(1, 3), _deauth_from_ap(2)])
    _associates(monkeypatch)

    campaign = await _run(VerifyPskCampaign, array, "pw")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.ERROR, "AP sent deauth (reason 2) after M2")


async def test_verify_psk_deauth_before_any_resent_m1_is_an_error(monkeypatch):
    array = _array(_iface())
    array.next_frame = AsyncMock(side_effect=[_eapol_from_ap(1, 1), _deauth_from_ap(15)])
    _associates(monkeypatch)

    campaign = await _run(VerifyPskCampaign, array, "pw")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.ERROR, "AP sent deauth (reason 15) after M2")


@pytest.mark.parametrize("acked, frames, error", [
    (True, [_eapol_from_ap(1, 1), None], "no reply after M2"),
    (False, [_eapol_from_ap(1, 1)], "AP never ACKed M2"),
    (True, [None], "AP never sent M1"),
])
async def test_verify_psk_errors(monkeypatch, acked, frames, error):
    array = _array(_iface(acked=acked))
    array.next_frame = AsyncMock(side_effect=frames)
    _associates(monkeypatch)

    campaign = await _run(VerifyPskCampaign, array, "pw")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.ERROR, error)


async def test_verify_errors_when_the_association_fails(monkeypatch):
    array = _array(_iface())
    _associates(monkeypatch, associated=False)

    campaign = await _run(VerifyPskCampaign, array, "pw")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.ERROR, "association rejected")


async def test_verify_errors_when_the_ap_is_not_heard():
    iface = _iface()
    array = _array(iface, heard=False)

    campaign = await _run(VerifyPskCampaign, array, "pw")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.ERROR, "AP not heard on channel 1")
    array.lease.assert_not_called()


async def test_verify_errors_when_the_card_cannot_ack_as_a_client():
    array = _array(_iface(), lease_mac=None)

    campaign = await _run(VerifyPskCampaign, array, "pw")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.ERROR, "this card can't ACK as a client")


async def test_a_crashed_exchange_is_an_error_and_still_sends_the_leaving_deauth(monkeypatch):
    iface = _iface()
    array = _array(iface)
    monkeypatch.setattr("wifit3.wlan.station.Association.associate",
                        AsyncMock(side_effect=OSError("USB gone")))

    campaign = await _run(VerifyPskCampaign, array, "pw")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.ERROR, "USB gone")
    leaving = iface.send_no_wait.await_args.args[0]
    assert (leaving[0], leaving[4:10], leaving[10:16]) == (0xC0, str_to_mac(BSSID), str_to_mac(OUR_MAC))


@pytest.mark.parametrize("result, outcome", [
    (PinResult.SUCCESS, VerifyOutcome.VERIFIED),
    (PinResult.FIRST_HALF_WRONG, VerifyOutcome.REJECTED),
    (PinResult.SECOND_HALF_WRONG, VerifyOutcome.REJECTED),
])
async def test_verify_pin(monkeypatch, result, outcome):
    array = _array(_iface())
    _associates(monkeypatch)
    try_pin = AsyncMock(return_value=AttemptOutcome(result, pin="12345670"))
    monkeypatch.setattr("wifit3.campaigns.verify.pin.WpsRegistrar.try_pin", try_pin)

    campaign = await _run(VerifyPinCampaign, array, "12345670")

    assert (campaign.outcome, campaign.error) == (outcome, None)
    try_pin.assert_awaited_once_with("12345670")


async def test_verify_pin_tries_an_empty_pin(monkeypatch):
    array = _array(_iface())
    _associates(monkeypatch)
    try_pin = AsyncMock(return_value=AttemptOutcome(PinResult.SUCCESS, pin=""))
    monkeypatch.setattr("wifit3.campaigns.verify.pin.WpsRegistrar.try_pin", try_pin)

    campaign = await _run(VerifyPinCampaign, array, "")

    assert campaign.outcome is VerifyOutcome.VERIFIED
    try_pin.assert_awaited_once_with("")


async def test_verify_pin_session_failure_is_an_error(monkeypatch):
    array = _array(_iface())
    _associates(monkeypatch)
    monkeypatch.setattr("wifit3.campaigns.verify.pin.WpsRegistrar.try_pin",
                        AsyncMock(return_value=AttemptOutcome(PinResult.PROTO_ERROR, pin="1", detail="AP locked")))

    campaign = await _run(VerifyPinCampaign, array, "12345670")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.ERROR, "AP locked")


def _wep_data_from_ap(key_hex: str) -> WepDataPacket:
    iv = b"\x01\x02\x03"
    plain = b"\xaa\xaa\x03\x00\x00\x00" + b"\x08\x06" + b"arp-body"
    blob = plain + icv(plain)
    keystream = rc4_keystream(iv + bytes.fromhex(key_hex), len(blob))
    return WepDataPacket(type="wep_data", type_id=2, subtype_id=0, bssid=BSSID, source=BSSID,
                         dest="ff:ff:ff:ff:ff:ff", to_ds=False, from_ds=True, rssi=-50, raw=b"\x00" * 30,
                         iv=iv, cipher=bytes(b ^ k for b, k in zip(blob, keystream)))


async def test_verify_wep_key_decrypting_passive_data_is_verified():
    array = _array(_iface())
    packet = _wep_data_from_ap("1234567890")

    async def next_frame(match, timeout):
        return packet if match(packet) else None

    array.next_frame = next_frame

    campaign = await _run(VerifyWepCampaign, array, "1234567890")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.VERIFIED, None)


async def test_verify_wep_key_that_decrypts_nothing_is_rejected(monkeypatch):
    array = _array(_iface())
    packet = _wep_data_from_ap("1234567890")

    async def next_frame(match, timeout):
        return packet if match(packet) else None

    array.next_frame = next_frame
    _associates(monkeypatch)
    monkeypatch.setattr("wifit3.campaigns.verify.wep.asyncio.sleep", AsyncMock())

    campaign = await _run(VerifyWepCampaign, array, "0987654321")

    assert (campaign.outcome, campaign.error) == (VerifyOutcome.REJECTED, None)


@pytest.mark.parametrize("campaign_class", [VerifyPskCampaign, VerifyPinCampaign, VerifyWepCampaign])
def test_verify_is_ineligible_on_a_hidden_ssid(campaign_class):
    assert campaign_class.ineligible_reason(AccessPoint(bssid=BSSID, ssid=None, channel=1)) == "hidden SSID: can't associate"
    assert campaign_class.ineligible_reason(AccessPoint(bssid=BSSID, ssid="TestNet", channel=1)) is None
