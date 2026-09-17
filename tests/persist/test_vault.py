"""Tests for the Vault: startup cache load, incremental cache updates on save,
and the session+persisted credential lookups (known_psk / has_psk).

Config.captures_dir is pointed at tmp_path by the autouse fixture in
tests/conftest.py, so Vault() and the save_* functions both land there."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from wifit3.models import AccessPoint, CaptureType, Handshake, HandshakeMessage
from wifit3.persist.config import Config
from wifit3.persist.vault import Vault


# ---- handshake fixtures (mirror tests/persist/test_save.py) -----------------

def _eapol_payload(mic: bytes = b"\xFF" * 16, key_data_len: int = 0) -> bytes:
    pl = bytearray(99 + key_data_len)
    pl[0] = 0x02
    pl[1] = 0x03
    pl[2:4] = (95 + key_data_len).to_bytes(2, "big")
    pl[4] = 0x02
    pl[5:7] = b"\x00\x8a"
    pl[81:97] = mic
    pl[97:99] = key_data_len.to_bytes(2, "big")
    return bytes(pl)


def _ef(msg_num: int, nonce: bytes, key_data_len: int = 0,
        payload_mic: bytes | None = None) -> HandshakeMessage:
    mic = b"\xAA" * 16
    return HandshakeMessage(
        raw=b"\x00" * 24, msg_num=msg_num, replay_hex=(5).to_bytes(8, "big").hex(),
        nonce=nonce, mic=mic, key_data_len=key_data_len,
        eapol_payload=_eapol_payload(mic=payload_mic or mic, key_data_len=key_data_len),
    )


def _ap_with_hs(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet",
                client_mac="11:22:33:44:55:66", *, pmkid=None) -> AccessPoint:
    ap = AccessPoint(bssid=bssid, ssid=ssid)
    hs = Handshake(bssid=bssid, client_mac=client_mac, beacon_frame=b"BEACON", pmkid=pmkid)
    hs.messages.extend([
        _ef(1, nonce=b"\xA0" + b"\x00" * 31, payload_mic=b"\x00" * 16),
        _ef(2, nonce=b"\xB0" + b"\x00" * 31, key_data_len=22),
    ])
    ap.handshakes[client_mac] = hs
    return ap


def _write_wps_pbc(d, bssid_dashed, ssid="Net", epoch=1700000000, psk="diskpsk"):
    (d / f"{ssid}_{bssid_dashed}_{epoch}_wps_pbc.txt").write_text(
        f"SSID: {ssid}\nBSSID: x\nPSK: {psk}\n", encoding="utf-8")


# ---- startup / refresh ------------------------------------------------------

def test_startup_loads_existing_captures(tmp_path):
    _write_wps_pbc(tmp_path, "00-11-22-33-44-88", psk="hunter2")
    v = Vault()
    caps = v.persisted("00:11:22:33:44:88")
    assert len(caps) == 1 and caps[0].type == CaptureType.WPS_PBC and caps[0].value == "hunter2"
    assert v.summary() == "1 WPS PSK"


def test_persisted_unknown_bssid_is_empty(tmp_path):
    assert Vault().persisted("aa:bb:cc:dd:ee:ff") == []


def test_summary_is_none_when_empty(tmp_path):
    assert Vault().summary() is None


def test_refresh_rescans_the_current_config_dir(tmp_path, monkeypatch):
    v = Vault()
    assert v.summary() is None
    other = tmp_path / "other"
    other.mkdir()
    _write_wps_pbc(other, "00-11-22-33-44-99")
    monkeypatch.setattr(Config, "captures_dir", str(other))
    v.refresh()
    assert v.summary() == "1 WPS PSK"


# ---- writes fold into the cache ---------------------------------------------

def test_save_wep_caches_key_and_dedupes(tmp_path):
    v = Vault()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
    r = v.save_wep_key(ap, b"abcde")
    assert r is not None and r.was_new
    caps = v.persisted(ap.bssid)
    assert len(caps) == 1 and caps[0].type == "WEP" and caps[0].value == b"abcde".hex()
    # A dedupe hit (was_new False) must not add a second cache entry.
    again = v.save_wep_key(ap, b"abcde")
    assert again is not None and not again.was_new
    assert len(v.persisted(ap.bssid)) == 1


def test_save_wps_pbc_and_pin_cache_their_psks(tmp_path):
    v = Vault()
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")
    v.save_wps_pbc(ap, "pbcpsk")
    v.save_wps_pin(ap, "12345670", "pinpsk")
    values = {c.value for c in v.persisted(ap.bssid)
              if c.type in (CaptureType.WPS_PIN, CaptureType.WPS_PBC)}
    assert values == {"pbcpsk", "pinpsk"}


def test_save_handshake_and_pmkid_cached_and_summarized(tmp_path):
    v = Vault()
    ap = _ap_with_hs(pmkid=b"\x11" * 16)
    assert v.save_handshake(ap, "11:22:33:44:55:66").was_new
    assert v.save_pmkid(ap, "11:22:33:44:55:66").was_new
    assert {c.type for c in v.persisted(ap.bssid)} == {"HS", "PMKID"}
    assert v.summary() == "1 handshake, 1 PMKID"


# ---- known_psk / has_psk: session fields OR a prior-session WPS file ---------

def test_known_psk_from_session_fields(tmp_path):
    v = Vault()
    ap = AccessPoint(bssid="00:11:22:33:44:55")
    assert v.has_psk(ap) is False and v.known_psk(ap) is None

    ap.wps_pbc_psk = "hunter2pbc"
    assert v.has_psk(ap) is True and v.known_psk(ap) == "hunter2pbc"

    ap2 = AccessPoint(bssid="00:11:22:33:44:66")
    ap2.wps_pin_psk = "hunter2pin"
    assert v.known_psk(ap2) == "hunter2pin"

    # A bare PIN with no recovered PSK does NOT count.
    ap3 = AccessPoint(bssid="00:11:22:33:44:77")
    ap3.wps_pin = "12345670"
    assert v.has_psk(ap3) is False and v.known_psk(ap3) is None


def test_known_psk_from_prior_session_wps_file(tmp_path):
    _write_wps_pbc(tmp_path, "00-11-22-33-44-88", psk="diskpsk")
    v = Vault()
    ap = AccessPoint(bssid="00:11:22:33:44:88")   # no session credential
    assert v.has_psk(ap) is True and v.known_psk(ap) == "diskpsk"


def test_non_wps_persisted_is_not_a_psk(tmp_path):
    v = Vault()
    ap = _ap_with_hs(bssid="00:11:22:33:44:99", ssid="Net")
    v.save_handshake(ap, "11:22:33:44:55:66")
    v.save_wep_key(ap, b"abcde")
    assert v.has_psk(ap) is False and v.known_psk(ap) is None


def test_kind_predicates(tmp_path):
    v = Vault()
    ap = _ap_with_hs(pmkid=b"\x11" * 16)
    assert not v.has_handshake(ap) and not v.has_pmkid(ap)
    v.save_handshake(ap, "11:22:33:44:55:66")
    v.save_pmkid(ap, "11:22:33:44:55:66")
    assert v.has_handshake(ap) and v.has_pmkid(ap)
    assert not v.has_wep_key(ap) and not v.has_wps_psk(ap)

    wep_ap = AccessPoint(bssid="00:11:22:33:44:aa", ssid="W")
    assert not v.has_wep_key(wep_ap)
    v.save_wep_key(wep_ap, b"abcde")
    assert v.has_wep_key(wep_ap)

    wps_ap = AccessPoint(bssid="00:11:22:33:44:bb", ssid="P")
    assert v.wps_capture(wps_ap) is None and not v.has_wps_psk(wps_ap)
    v.save_wps_pbc(wps_ap, "hunter2")
    assert v.has_wps_psk(wps_ap)
    cap = v.wps_capture(wps_ap)
    assert cap is not None and cap.value == "hunter2"


def test_detailed_summary(tmp_path):
    v = Vault()
    ap = _ap_with_hs(pmkid=b"\x11" * 16)
    assert v.detailed_summary(ap) == {}
    v.save_handshake(ap, "11:22:33:44:55:66")
    v.save_pmkid(ap, "11:22:33:44:55:66")
    v.save_wep_key(ap, b"abcde")
    v.save_wps_pin(ap, "12345670", "pinpsk")
    v.save_wps_pbc(ap, "hunter2")
    summary = v.detailed_summary(ap)
    assert list(summary) == ["Handshake", "PMKID", "WEP Key", "WPS PIN", "WPS PBC"]  # kind order
    assert summary["Handshake"][0].value is None and summary["PMKID"][0].value is None
    assert summary["WEP Key"][0].value == b"abcde".hex()
    pin_cap, pin_count = summary["WPS PIN"]
    assert pin_cap.value == "pinpsk" and pin_cap.pin == "12345670" and pin_count == 1
    assert summary["WPS PBC"][0].value == "hunter2"


# ---- all_captures / delete / zip / open_directory / capture_payload ----------

def test_all_captures_flat_across_aps(tmp_path):
    v = Vault()
    v.save_wep_key(AccessPoint(bssid="00:11:22:33:44:01", ssid="A"), b"abcde")
    v.save_wps_pbc(AccessPoint(bssid="00:11:22:33:44:02", ssid="B"), "pbcpsk")
    caps = v.all_captures()
    assert len(caps) == 2
    assert {c.bssid for c in caps} == {"00:11:22:33:44:01", "00:11:22:33:44:02"}


def test_delete_capture_removes_file_and_cache_entry(tmp_path):
    v = Vault()
    ap = AccessPoint(bssid="00:11:22:33:44:01", ssid="A")
    r = v.save_wep_key(ap, b"abcde")
    assert Path(r.path).exists()
    v.delete_capture(v.persisted(ap.bssid)[0])
    assert not Path(r.path).exists()
    assert v.persisted(ap.bssid) == []


def test_delete_capture_drops_all_entries_sharing_a_path(tmp_path):
    # An aggregate .hc22000 backs both an HS and a PMKID entry: deleting drops both.
    agg = tmp_path / "Net_00-11-22-33-44-03.hc22000"
    agg.write_text("WPA*01*x\nWPA*02*y\n", encoding="utf-8")
    v = Vault()
    caps = v.persisted("00:11:22:33:44:03")
    assert {c.type for c in caps} == {CaptureType.HS, CaptureType.PMKID}
    v.delete_capture(caps[0])
    assert v.persisted("00:11:22:33:44:03") == []
    assert not agg.exists()


def test_zip_captures_bundles_beside_dir(tmp_path):
    v = Vault()
    v.save_wep_key(AccessPoint(bssid="00:11:22:33:44:01", ssid="A"), b"abcde")
    out = v.zip_captures(v.all_captures())
    assert out is not None and out.parent == tmp_path.parent
    with zipfile.ZipFile(out) as zf:
        assert len(zf.namelist()) == 1


def test_zip_captures_none_when_empty(tmp_path):
    assert Vault().zip_captures([]) is None


def test_zip_captures_dedupes_shared_path(tmp_path):
    agg = tmp_path / "Net_00-11-22-33-44-03.hc22000"
    agg.write_text("WPA*01*x\nWPA*02*y\n", encoding="utf-8")
    v = Vault()
    out = v.zip_captures(v.persisted("00:11:22:33:44:03"))
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == [agg.name]


def test_open_directory_uses_platform_launcher(tmp_path, mocker):
    mocker.patch.object(sys, "platform", "linux")
    popen = mocker.patch("wifit3.persist.vault.subprocess.Popen")
    Vault().open_directory()
    popen.assert_called_once_with(["xdg-open", str(tmp_path)])


def test_capture_payload_value_then_file(tmp_path):
    v = Vault()
    ap = _ap_with_hs()
    v.save_handshake(ap, "11:22:33:44:55:66")
    hs_cap = v.persisted(ap.bssid)[0]
    Path(hs_cap.path).write_text("WPA*02*deadbeef\n", encoding="utf-8")
    assert v.capture_payload(hs_cap) == "WPA*02*deadbeef"
    v.save_wep_key(ap, b"abcde")
    wep_cap = next(c for c in v.persisted(ap.bssid) if c.type == CaptureType.WEP)
    assert v.capture_payload(wep_cap) == b"abcde".hex()
