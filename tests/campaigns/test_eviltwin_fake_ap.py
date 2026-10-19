"""FakeAP responder as a state machine: feed parsed client frames, assert responses + stats."""
import asyncio

import pytest

from wifit3.campaigns.eviltwin import FakeAP, ClientPhase
from wifit3.dot11.parser import WlanFrameParser
from wifit3.dot11.probe import probe_req
from wifit3.dot11.ap import auth_resp
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


async def test_probe_resp_advertises_the_beacon_minus_tim():
    head = b"\x80\x00\x00\x00" + b"\xff" * 6 + _BSSID + _BSSID + b"\x00\x00"
    fixed = bytes(8) + b"\x64\x00" + b"\x11\x04"                 # TSF, 100 TU, capability
    ssid_ie, tim_ie = b"\x00\x07" + _SSID.encode(), b"\x05\x04\x00\x01\x00\x00"
    beacon = head + fixed + ssid_ie + tim_ie + GENERIC_RSN_IE
    fap = FakeAP(FakeIface(), _BSSID, _SSID, 1, twin_beacon=beacon)
    fap.on_rx(_parse(probe_req(_BSSID, _CLIENT, _SSID)))
    await _flush()
    resp = fap.iface.sent[0]
    assert resp[:2] == b"\x50\x00" and resp[4:10] == _CLIENT and resp[10:22] == _BSSID * 2
    assert resp[32:36] == fixed[8:]                               # same interval + capability
    assert resp[36:] == ssid_ie + GENERIC_RSN_IE                  # same elements, TIM dropped
    assert resp[24:32] != bytes(8)                                # TSF stamped at send


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


async def test_m1_reaches_the_driver_after_the_assoc_resp():
    class SlowMgmtIface(FakeIface):
        async def send_no_wait(self, frame: bytes) -> bool:
            if frame[0] & 0x0C == 0:                            # management frames take longer
                for _ in range(5):
                    await asyncio.sleep(0)
            return await super().send_no_wait(frame)

    fap = FakeAP(SlowMgmtIface(), _BSSID, _SSID, 1, twin_beacon=bytes(60))
    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)))
    for _ in range(20):
        await asyncio.sleep(0)
    kinds = [f[:2] for f in fap.iface.sent]
    assert kinds.index(b"\x10\x00") < kinds.index(b"\x08\x02")  # assoc-resp, then M1


async def test_reassoc_req_gets_a_reassoc_resp():
    fap = _fakeap()
    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    req = assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)
    fap.on_rx(_parse(b"\x20\x00" + req[2:28] + _BSSID + req[28:]))   # + current AP address
    await _flush()
    kinds = [f[:2] for f in fap.iface.sent]
    assert b"\x30\x00" in kinds and b"\x10\x00" not in kinds


async def test_assoc_resp_carries_the_twin_beacon_ht_elements():
    ht_op = bytes.fromhex("3d03" "010000")
    fap = FakeAP(FakeIface(), _BSSID, _SSID, 1, twin_beacon=bytes(36) + ht_op)
    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)))
    await _flush()
    resp = next(f for f in fap.iface.sent if f[:2] == b"\x10\x00")
    assert resp.endswith(ht_op)


async def test_cached_sae_key_assoc_is_refused_with_status_53():
    fap = _fakeap()
    sae_rsn_with_pmkid = bytes.fromhex("30260100000fac040100000fac040100000fac08c0000100") + bytes(16)
    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, sae_rsn_with_pmkid)))
    await _flush()
    assert fap.stats.cached_key_rejects == 1
    resp = [f for f in fap.iface.sent if f[:2] == b"\x10\x00"]
    assert len(resp) == 1 and resp[0][26:28] == (53).to_bytes(2, "little")
    assert not any(f[:2] == b"\x08\x02" for f in fap.iface.sent)  # no M1
    assert fap.stats.clients["02:aa:bb:cc:dd:ee"].phase == ClientPhase.AUTHED


async def test_sae_auth_is_refused_with_status_13():
    fap = _fakeap()
    sae_commit = auth_req(_BSSID, _CLIENT)[:24] + b"\x03\x00\x01\x00\x7e\x00" + b"\x13\x00" + bytes(96)
    fap.on_rx(_parse(sae_commit))
    await _flush()
    assert fap.stats.sae_auth_rejects == 1
    assert fap.iface.sent == [auth_resp(_BSSID, _CLIENT, algorithm=3, status=13)]
    assert fap.iface.sent[0][24:30] == b"\x03\x00\x02\x00\x0d\x00"   # SAE, seq 2, status 13
    assert "02:aa:bb:cc:dd:ee" not in fap.stats.clients


async def test_m2_marks_client_once():
    fap = _fakeap()
    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)))
    m2 = _m2(_BSSID, _CLIENT, bytes(range(32, 64)))
    fap.on_rx(_parse(m2))
    fap.on_rx(_parse(m2))                                       # duplicate M2
    assert fap.stats.m2 == 1
    assert fap.stats.clients["02:aa:bb:cc:dd:ee"].phase == ClientPhase.GOT_M2


async def test_every_stray_data_frame_gets_a_deauth():
    fap = _fakeap()
    data = data_header(to_ds=True, bssid=_BSSID, client=_CLIENT) + b"\x00\x00payload"
    fap.on_rx(_parse(data))
    fap.on_rx(_parse(data))                                     # a second stray data frame
    await _flush()
    assert fap.stats.strays == 2
    assert len(fap.iface.sent) == 2
    sent = fap.iface.sent[0]
    assert sent[0] == 0xC0 and sent[4:10] == _CLIENT            # deauth AP->client
    assert sent[10:16] == _BSSID and sent[24] == 7              # from the twin, reason 7


async def test_data_from_an_associated_client_is_not_deauthed():
    fap = _fakeap()
    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)))
    await _flush()
    fap.iface.sent.clear()
    fap.on_rx(_parse(data_header(to_ds=True, bssid=_BSSID, client=_CLIENT) + b"\x00\x00x"))
    await _flush()
    assert fap.stats.strays == 0 and not any(f[0] == 0xC0 for f in fap.iface.sent)


async def test_m2_to_another_bssid_ignored():
    fap = _fakeap()
    other_ap = bytes.fromhex("020000000099")
    fap.on_rx(_parse(_m2(other_ap, _CLIENT, bytes(range(32, 64)))))   # a real AP's handshake on our channel
    assert fap.stats.m2 == 0
    assert not fap.stats.clients


async def test_auth_to_a_different_bssid_ignored():
    fap = _fakeap()
    fap.on_rx(_parse(auth_req(_OTHER, _CLIENT)))
    await _flush()
    assert fap.stats.auth == 0 and fap.stats.clients == {} and fap.iface.sent == []


class _LifecycleIface(FakeIface):
    def __init__(self, clear_error=None):
        super().__init__()
        self.clears = 0
        self._clear_error = clear_error

    async def clear_fake_mac(self) -> None:
        self.clears += 1
        if self._clear_error is not None:
            raise self._clear_error


class _RxSource:
    def __init__(self):
        self.registered = 0

    def register_rx_callback(self, cb) -> None:
        self.registered += 1

    def unregister_rx_callback(self, cb) -> None:
        self.registered -= 1


async def test_stop_is_idempotent_and_reaps_the_beacon_task():
    iface, rx = _LifecycleIface(), _RxSource()
    fap = FakeAP(iface, _BSSID, _SSID, 1, twin_beacon=bytes(60), rx_source=rx)
    await fap.start()
    await _flush()
    task = fap._beacon_task
    await fap.stop()
    await fap.stop()
    assert task.done() and fap._beacon_task is None
    assert rx.registered == 0 and iface.clears == 1


async def test_stop_surfaces_a_teardown_failure_after_cleaning_up():
    iface, rx = _LifecycleIface(clear_error=RuntimeError("clear failed")), _RxSource()
    fap = FakeAP(iface, _BSSID, _SSID, 1, twin_beacon=bytes(60), rx_source=rx)
    await fap.start()
    with pytest.raises(RuntimeError, match="clear failed"):
        await fap.stop()
    assert rx.registered == 0


@pytest.fixture
def sleeps(monkeypatch):
    """Every asyncio.sleep returns after one loop turn; the requested delays are recorded."""
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def fast_sleep(delay, result=None):
        delays.append(delay)
        await real_sleep(0)
        return result

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    return delays


async def _turns(n: int = 50) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _sent_m1s(fap) -> list:
    return [_parse(f) for f in fap.iface.sent if f[:2] == b"\x08\x02"]


async def test_m1_is_resent_with_the_same_anonce_and_the_next_replay_counter(sleeps):
    m1s: list[bytes] = []
    fap = _fakeap(m1_sink=m1s.append)
    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)))
    await _turns()
    sent = _sent_m1s(fap)
    assert [int.from_bytes(m.replay_counter, "big") for m in sent] == [1, 2, 3, 4]
    assert {m.nonce for m in sent} == {fap.stats.clients["02:aa:bb:cc:dd:ee"].anonce}
    assert [d for d in sleeps if d in (0.1, 1.0)] == [0.1, 1.0, 1.0]
    assert [_parse(f).replay_counter for f in m1s] == [m.replay_counter for m in sent]
    assert not fap._m1_tasks


async def test_m2_stops_the_m1_resends(sleeps):
    fap = _fakeap()
    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)))
    while len(_sent_m1s(fap)) < 2:
        await asyncio.sleep(0)
    fap.on_rx(_parse(_m2(_BSSID, _CLIENT, bytes(range(32, 64)))))
    await _turns()
    assert len(_sent_m1s(fap)) == 2 and not fap._m1_tasks


async def test_reassoc_starts_a_fresh_m1_sequence(sleeps):
    fap = _fakeap()
    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)))
    while len(_sent_m1s(fap)) < 2:
        await asyncio.sleep(0)
    first_anonce = fap.stats.clients["02:aa:bb:cc:dd:ee"].anonce
    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)))
    await _turns()
    sent = _sent_m1s(fap)
    assert [m.nonce for m in sent].count(first_anonce) == 2
    fresh = [int.from_bytes(m.replay_counter, "big") for m in sent if m.nonce != first_anonce]
    assert fresh == [1, 2, 3, 4]


async def test_stop_cancels_the_pending_m1_resends(sleeps):
    iface, rx = _LifecycleIface(), _RxSource()
    fap = FakeAP(iface, _BSSID, _SSID, 1, twin_beacon=bytes(60), rx_source=rx)
    await fap.start()
    fap.on_rx(_parse(auth_req(_BSSID, _CLIENT)))
    fap.on_rx(_parse(assoc_req(_BSSID, _CLIENT, _SSID, GENERIC_RSN_IE)))
    task = fap._m1_tasks["02:aa:bb:cc:dd:ee"]
    await fap.stop()
    await fap.stop()
    sent = len(_sent_m1s(fap))
    await _turns()
    assert task.cancelled() and not fap._m1_tasks
    assert len(_sent_m1s(fap)) == sent <= 1
