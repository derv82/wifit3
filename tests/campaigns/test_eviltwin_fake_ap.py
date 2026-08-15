"""FakeAP responder as a state machine: feed parsed client frames, assert responses + stats."""
import asyncio

from wifit3.campaigns.eviltwin import FakeAP, ClientPhase
from wifit3.dot11.parser import WlanFrameParser
from wifit3.dot11.probe import probe_req
from wifit3.dot11.auth_assoc import auth_req, assoc_req
from wifit3.dot11.eapol import eapol_key, data_header, LLC_SNAP_EAPOL
from wifit3.dot11.ie import GENERIC_RSN_IE

_BSSID = bytes.fromhex("9483c48c3f78")
_CLIENT = bytes.fromhex("02aabbccddee")
_OTHER = bytes.fromhex("001122334455")
_SSID = "GL-Test"


class FakeIface:
    FAKE_MAC = None

    def __init__(self, channel: int = 1):
        self.sent: list[bytes] = []
        self.current_channel = channel

    async def send_no_wait(self, frame: bytes) -> bool:
        self.sent.append(frame)
        return True

    async def set_channel(self, channel: int, scan: bool = False) -> bool:
        self.current_channel = channel
        return True

    async def set_fake_mac(self, mac=None, bssid=None):
        return ":".join(f"{b:02x}" for b in bssid)

    async def clear_fake_mac(self) -> None:
        pass


def _parse(frame: bytes):
    return WlanFrameParser.parse_80211_frame(frame, 0)


def _m2(bssid: bytes, client: bytes, snonce: bytes) -> bytes:
    payload = eapol_key(key_info=0x010A, key_len=0, replay=1, nonce=snonce, key_data=GENERIC_RSN_IE)
    return data_header(to_ds=True, bssid=bssid, client=client) + LLC_SNAP_EAPOL + payload


def _fakeap(m1_sink=None):
    return FakeAP(FakeIface(), _BSSID, _SSID, 1, twin_beacon=bytes(60), record_m1=m1_sink)


async def _flush():
    await asyncio.sleep(0.01)


async def test_wildcard_probe_answered_and_counted():
    fap = _fakeap()
    fap.on_rx(_parse(probe_req(_BSSID, _CLIENT, "")))
    await _flush()
    assert fap.stats.probes_wildcard == 1 and fap.stats.probes_direct == 0
    assert len(fap.iface.sent) == 1
    resp = fap.iface.sent[0]
    assert resp[:2] == b"\x50\x00" and resp[4:10] == _CLIENT     # probe resp addressed to the client


async def test_directed_probe_for_us_counted():
    fap = _fakeap()
    fap.on_rx(_parse(probe_req(_BSSID, _CLIENT, _SSID)))
    await _flush()
    assert fap.stats.probes_direct == 1 and len(fap.iface.sent) == 1


async def test_probe_for_other_ssid_ignored():
    fap = _fakeap()
    fap.on_rx(_parse(probe_req(_BSSID, _CLIENT, "SomeoneElse")))
    await _flush()
    assert fap.stats.probes_direct == 0 and fap.stats.probes_wildcard == 0
    assert fap.iface.sent == []


async def test_auth_assoc_drives_m1_and_progress():
    m1s: list[bytes] = []
    fap = _fakeap(m1_sink=m1s.append)
    cs = "02:aa:bb:cc:dd:ee"

    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    assert fap.stats.auth == 1
    assert fap.stats.clients[cs].phase == ClientPhase.AUTHED

    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)))
    await _flush()
    rec = fap.stats.clients[cs]
    assert fap.stats.assoc == 1 and rec.phase == ClientPhase.ASSOCED and len(rec.anonce) == 32
    assert len(m1s) == 1 and m1s[0][:2] == b"\x08\x02"          # M1 seeded before injection
    assert m1s[0] in fap.iface.sent                             # and also injected
    assert any(f[:2] == b"\x10\x00" for f in fap.iface.sent)    # assoc resp went out


async def test_m2_marks_client_once():
    fap = _fakeap()
    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)))
    m2 = _m2(_BSSID, _CLIENT, bytes(range(32, 64)))
    fap.on_rx(_parse(m2))
    fap.on_rx(_parse(m2))                                       # duplicate M2
    assert fap.stats.m2 == 1
    assert fap.stats.clients["02:aa:bb:cc:dd:ee"].phase == ClientPhase.GOT_M2


async def test_auth_to_a_different_bssid_ignored():
    fap = _fakeap()
    fap.on_rx(_parse(auth_req(_OTHER, _CLIENT)))
    await _flush()
    assert fap.stats.auth == 0 and fap.stats.clients == {} and fap.iface.sent == []
