from __future__ import annotations

from wifit3.models import AccessPoint, Handshake, HandshakeMessage
from wifit3.models.jobs import ToolStatus
from wifit3.persist.config import Config
from wifit3.persist.vault import Vault
from wifit3.vault.tools.hashtopolis import line_digest


def _configure(monkeypatch, *, auto=True, url="http://ht.local", token="tok"):
    monkeypatch.setattr(Config, "hashtopolis_url", url)
    monkeypatch.setattr(Config, "hashtopolis_token", token)
    monkeypatch.setattr(Config, "hashtopolis_auto_submit", auto)



def _eapol_payload(mic=b"\xFF" * 16, key_data_len=0):
    pl = bytearray(99 + key_data_len)
    pl[0] = 0x02
    pl[1] = 0x03
    pl[2:4] = (95 + key_data_len).to_bytes(2, "big")
    pl[4] = 0x02
    pl[5:7] = b"\x00\x8a"
    pl[81:97] = mic
    pl[97:99] = key_data_len.to_bytes(2, "big")
    return bytes(pl)


def _ef(msg_num, nonce, key_data_len=0, payload_mic=None):
    mic = b"\xAA" * 16
    return HandshakeMessage(
        raw=b"\x00" * 24, msg_num=msg_num, replay_hex=(5).to_bytes(8, "big").hex(),
        nonce=nonce, mic=mic, key_data_len=key_data_len,
        eapol_payload=_eapol_payload(mic=payload_mic or mic, key_data_len=key_data_len))


def _ap_with_hs(anonce=b"\xA0" + b"\x00" * 31, pmkid=None, with_pair=True):
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
    hs = Handshake(bssid=ap.bssid, client_mac="11:22:33:44:55:66", beacon_frame=b"BEACON", pmkid=pmkid)
    if with_pair:
        hs.messages.extend([
            _ef(1, nonce=anonce, payload_mic=b"\x00" * 16),
            _ef(2, nonce=b"\xB0" + b"\x00" * 31, key_data_len=22)])
    ap.handshakes["11:22:33:44:55:66"] = hs
    return ap


def _ht_jobs(vault):
    return [j for j in vault.manager.jobs.values() if j.tool_name == "hashtopolis"]



def test_default_off_enqueues_nothing(monkeypatch, tmp_path):
    _configure(monkeypatch, auto=False)
    v = Vault()
    assert v.save_handshake(_ap_with_hs(), "11:22:33:44:55:66").was_new
    assert _ht_jobs(v) == []


def test_not_configured_enqueues_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "hashtopolis_auto_submit", True)
    monkeypatch.setattr(Config, "hashtopolis_url", None)
    v = Vault()
    assert v.save_handshake(_ap_with_hs(), "11:22:33:44:55:66").was_new
    assert _ht_jobs(v) == []


def test_handshake_enqueues_new_lines(monkeypatch, tmp_path):
    _configure(monkeypatch, auto=True)
    v = Vault()
    result = v.save_handshake(_ap_with_hs(), "11:22:33:44:55:66")
    jobs = _ht_jobs(v)
    assert len(jobs) == 1 and jobs[0].status == ToolStatus.QUEUED
    assert jobs[0].config["line_digests"] == [line_digest(ln) for ln in result.new_hashlines]


def test_pmkid_enqueues(monkeypatch, tmp_path):
    _configure(monkeypatch, auto=True)
    v = Vault()
    result = v.save_pmkid(_ap_with_hs(pmkid=b"\x11" * 16, with_pair=False), "11:22:33:44:55:66")
    assert result.was_new
    assert len(_ht_jobs(v)) == 1


def test_dedupe_hit_does_not_enqueue(monkeypatch, tmp_path):
    _configure(monkeypatch, auto=True)
    v = Vault()
    v.save_handshake(_ap_with_hs(anonce=b"\xA0" + b"\x00" * 31), "11:22:33:44:55:66")
    again = v.save_handshake(_ap_with_hs(anonce=b"\xA0" + b"\x00" * 31), "11:22:33:44:55:66")
    assert again.was_new is False
    assert len(_ht_jobs(v)) == 1


def test_append_enqueues_only_the_new_line(monkeypatch, tmp_path):
    _configure(monkeypatch, auto=True)
    v = Vault()
    v.save_handshake(_ap_with_hs(anonce=b"\xA0" + b"\x00" * 31), "11:22:33:44:55:66")
    v.save_handshake(_ap_with_hs(anonce=b"\xC0" + b"\x00" * 31), "11:22:33:44:55:66")
    jobs = _ht_jobs(v)
    assert len(jobs) == 2
    assert all(len(j.config["line_digests"]) == 1 for j in jobs)


def test_save_path_never_calls_the_network(monkeypatch, tmp_path):
    _configure(monkeypatch, auto=True)
    v = Vault()

    def _boom():
        raise AssertionError("save path must not build a Hashtopolis client")

    monkeypatch.setattr(v.manager.tools["hashtopolis"], "_client_factory", _boom)
    v.save_handshake(_ap_with_hs(), "11:22:33:44:55:66")
    assert len(_ht_jobs(v)) == 1
