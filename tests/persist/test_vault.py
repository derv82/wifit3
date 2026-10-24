"""Tests for the Vault: startup cache load, incremental cache updates on save,
and the session+persisted credential lookups (known_psk / has_psk).

Config.captures_dir is pointed at tmp_path by the autouse fixture in
tests/conftest.py, so Vault() and the save_* functions both land there."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

from wifit3.crack import wpa_psk
from wifit3.crack.hc22000_format import Hc22000Line
from wifit3.dot11.wpa import derive_pmk
from wifit3.models import AccessPoint, CaptureType, Handshake, HandshakeMessage, ToolName
from wifit3.persist.config import Config
from wifit3.persist.meta import update_meta
from wifit3.persist.vault import Vault, crosses_auto_crack_threshold


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


def _write_wpa_psk(d, bssid_dashed, ssid="Net", epoch=1700000001, psk="crackedpw"):
    (d / f"{ssid}_{bssid_dashed}_{epoch}_wpa_psk.txt").write_text(
        f"SSID: {ssid}\nBSSID: x\nPSK: {psk}\n", encoding="utf-8")


# ---- startup / refresh ------------------------------------------------------

def test_startup_loads_existing_captures(tmp_path):
    _write_wps_pbc(tmp_path, "00-11-22-33-44-88", psk="hunter2")
    v = Vault()
    caps = v.persisted("00:11:22:33:44:88")
    assert len(caps) == 1 and caps[0].type == CaptureType.WPS_PBC and caps[0].value == "hunter2"
    assert v.summary() == "1 PSK"


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
    assert v.summary() == "1 PSK"


# ---- writes fold into the cache ---------------------------------------------

def test_vault_notifies_subscribers_only_when_capture_index_changes(tmp_path):
    vault = Vault()
    changes = []
    vault.subscribe(lambda: changes.append(vault.revision))
    ap = AccessPoint(bssid="aa:bb:cc:dd:ee:ff", ssid="HomeNet")

    vault.save_wep_key(ap, b"abcde")
    vault.save_wep_key(ap, b"abcde")

    assert changes == [2]


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


def test_a_pin_only_capture_is_not_a_psk(tmp_path):
    ap = AccessPoint(bssid="00:11:22:33:44:99", ssid="Net")
    Vault().save_wps_pin(ap, "12345670", None)
    v = Vault()
    caps = v.persisted(ap.bssid)
    assert len(caps) == 1 and caps[0].pin == "12345670" and caps[0].value is None
    assert not v.has_psk(ap) and not v.has_wps_psk(ap) and v.summary() is None


def test_known_psk_from_cracked_wpa_psk_file(tmp_path):
    _write_wpa_psk(tmp_path, "00-11-22-33-44-aa", psk="crackedpw")
    v = Vault()
    ap = AccessPoint(bssid="00:11:22:33:44:aa")   # never had WPS, cracked from a handshake
    assert v.has_psk(ap) is True and v.known_psk(ap) == "crackedpw"


def test_wpa_psk_has_a_kind_label():
    assert CaptureType.WPA_PSK in Vault._KIND_LABELS


def test_vault_records_error_when_index_load_fails(tmp_path, monkeypatch):
    import wifit3.persist.vault as vault_mod

    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(vault_mod, "load_capture_index", boom)
    v = Vault()                       # must not raise despite the failing scan
    assert v._index == {}
    assert any("disk gone" in e for e in v.errors)


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


def test_delete_capture_removes_the_meta_with_the_last_capture(tmp_path):
    v = Vault()
    ap = AccessPoint(bssid="00:11:22:33:44:01", ssid="A")
    v.save_wep_key(ap, b"abcde")
    v.save_wep_key(ap, b"fghij")
    meta = tmp_path / "A_00-11-22-33-44-01.meta"
    v.delete_capture(v.persisted(ap.bssid)[0])
    assert meta.exists()
    v.delete_capture(v.persisted(ap.bssid)[0])
    assert not meta.exists()


def test_delete_capture_keeps_the_meta_of_another_ssid_on_the_bssid(tmp_path):
    v = Vault()
    v.save_wep_key(AccessPoint(bssid="00:11:22:33:44:01", ssid="A"), b"abcde")
    v.save_wep_key(AccessPoint(bssid="00:11:22:33:44:01", ssid="B"), b"fghij")
    v.delete_capture(next(c for c in v.persisted("00:11:22:33:44:01") if c.ssid == "A"))
    assert not (tmp_path / "A_00-11-22-33-44-01.meta").exists()
    assert (tmp_path / "B_00-11-22-33-44-01.meta").exists()


def test_every_save_writes_the_meta(tmp_path):
    v = Vault()
    ap = _ap_with_hs(pmkid=b"" * 16)
    v.save_handshake(ap, "11:22:33:44:55:66")
    v.save_pmkid(ap, "11:22:33:44:55:66")
    v.save_wps_pin(AccessPoint(bssid="00:11:22:33:44:02", ssid="Pin"), "12345670", "pw123456")
    v.save_wps_pbc(AccessPoint(bssid="00:11:22:33:44:03", ssid="Pbc"), "pw123456")
    v.save_wpa_psk("Psk", "00:11:22:33:44:04", "pw123456")
    v.save_wep_key(AccessPoint(bssid="00:11:22:33:44:05", ssid="Wep"), b"abcde")
    assert sorted(p.name for p in tmp_path.glob("*.meta")) == [
        "HomeNet_aa-bb-cc-dd-ee-ff.meta", "Pbc_00-11-22-33-44-03.meta",
        "Pin_00-11-22-33-44-02.meta", "Psk_00-11-22-33-44-04.meta",
        "Wep_00-11-22-33-44-05.meta"]


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
        names = zf.namelist()
    assert len(names) == 2 and "A_00-11-22-33-44-01.meta" in names


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


def test_open_directory_raises_file_not_found_before_the_first_capture(tmp_path, mocker,
                                                                      monkeypatch):
    mocker.patch.object(sys, "platform", "linux")
    popen = mocker.patch("wifit3.persist.vault.subprocess.Popen")
    monkeypatch.setattr(Config, "captures_dir", str(tmp_path / "captures"))
    with pytest.raises(FileNotFoundError):
        Vault().open_directory()
    popen.assert_not_called()


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


# ---- auto-crack threshold ----------------------------------------------------

def _line(kind: str, message_pair: str = "") -> Hc22000Line:
    return Hc22000Line(kind=kind, pmkid_or_mic="00", mac_ap="aabbccddeeff",
                       mac_sta="112233445566", essid="4e6574", message_pair=message_pair)


_M1M2 = _line("02", "00")
_M1M2_FLAGGED = _line("02", "80")
_M2M3 = _line("02", "02")
_M3M4 = _line("02", "05")
_PMKID = _line("01")


def test_threshold_first_line_for_an_ap():
    assert crosses_auto_crack_threshold([], [_M1M2])
    assert crosses_auto_crack_threshold([], [_PMKID])
    assert not crosses_auto_crack_threshold([], [])


def test_threshold_first_authorized_eapol_after_m1m2():
    assert crosses_auto_crack_threshold([_M1M2], [_M1M2, _M2M3])
    assert crosses_auto_crack_threshold([_M1M2_FLAGGED], [_M1M2_FLAGGED, _M3M4])


def test_threshold_first_pmkid_after_m1m2():
    assert crosses_auto_crack_threshold([_M1M2], [_M1M2, _PMKID])


def test_threshold_repeated_handshakes_do_not_cross():
    assert not crosses_auto_crack_threshold([_M1M2], [_M1M2, _M1M2_FLAGGED])
    assert not crosses_auto_crack_threshold([_M1M2, _M2M3], [_M1M2, _M2M3, _M3M4])


def test_threshold_prior_authorized_line_never_crosses():
    assert not crosses_auto_crack_threshold([_M2M3], [_M2M3, _PMKID])
    assert not crosses_auto_crack_threshold([_PMKID], [_PMKID, _M2M3])


# ---- auto-crack trigger on save ------------------------------------------------

_CLIENT = "11:22:33:44:55:66"


def _auto_crack_call(capture, extra_passwords: list[str] | None = None) -> tuple:
    return (("cpu_crack", capture, {"wordlists": [], "default_passwords": True,
                                    "ap_ssid_passwords": True, "probed_ssid_passwords": True,
                                    "extra_passwords": extra_passwords or []}),
            {"auto_crack": True})


def test_save_submits_cpu_crack_once_per_threshold(tmp_path, mocker):
    v = Vault()
    mocker.patch.object(v.manager, "submit_job")
    ap = _ap_with_hs(pmkid=b"\x11" * 16)

    v.save_handshake(ap, _CLIENT)
    assert v.manager.submit_job.call_args_list == [_auto_crack_call(v.persisted(ap.bssid)[0])]

    ap.handshakes[_CLIENT].messages[0] = _ef(1, nonce=b"\xA1" + b"\x00" * 31,
                                             payload_mic=b"\x00" * 16)
    assert v.save_handshake(ap, _CLIENT).was_new
    assert v.manager.submit_job.call_count == 1

    v.save_pmkid(ap, _CLIENT)
    assert v.manager.submit_job.call_count == 2
    assert v.manager.submit_job.call_args == _auto_crack_call(v.persisted(ap.bssid)[0])


def test_auto_crack_snapshots_probed_ssids_sorted(tmp_path, mocker):
    v = Vault()
    mocker.patch.object(v.manager, "submit_job")
    probed_ssids = {"Zeta_SSID", "Alpha_SSID"}
    v.set_probed_ssids_source(lambda: probed_ssids)

    v.save_handshake(_ap_with_hs(), _CLIENT)
    probed_ssids.add("Later_SSID")

    capture = v.persisted(_ap_with_hs().bssid)[0]
    assert v.manager.submit_job.call_args == _auto_crack_call(capture, ["Alpha_SSID", "Zeta_SSID"])


@pytest.mark.parametrize("ticked", [True, False])
def test_add_probed_ssids_only_when_the_job_asks_for_them(tmp_path, ticked):
    v = Vault()
    v.set_probed_ssids_source(lambda: {"Zeta_SSID", "Alpha_SSID"})
    config = {"wordlists": [], "probed_ssid_passwords": ticked}
    expected = {**config, "extra_passwords": ["Alpha_SSID", "Zeta_SSID"]} if ticked else config
    assert v.add_probed_ssids(config) == expected


def test_save_does_not_submit_when_auto_crack_is_off(tmp_path, mocker, monkeypatch):
    monkeypatch.setattr(Config, "auto_crack", None)
    v = Vault()
    mocker.patch.object(v.manager, "submit_job")

    v.save_handshake(_ap_with_hs(), _CLIENT)

    v.manager.submit_job.assert_not_called()


def test_save_does_not_submit_when_the_psk_is_known(tmp_path, mocker):
    v = Vault()
    mocker.patch.object(v.manager, "submit_job")
    ap = _ap_with_hs()
    ap.wps_pbc_psk = "knownpsk"

    v.save_handshake(ap, _CLIENT)

    v.manager.submit_job.assert_not_called()


def test_save_submits_to_the_chosen_tool_with_its_saved_settings(tmp_path, mocker, monkeypatch):
    monkeypatch.setattr(Config, "auto_crack", ToolName.HASHCAT)
    monkeypatch.setattr(Config.hashcat, "path", "/usr/bin/hashcat")
    monkeypatch.setattr(Config.hashcat, "wordlists", ["/lists/a.txt"])
    monkeypatch.setattr(Config.hashcat, "args", "-w 3")
    v = Vault()
    mocker.patch.object(v.manager, "submit_job")

    v.save_handshake(_ap_with_hs(), _CLIENT)

    capture = v.persisted(_ap_with_hs().bssid)[0]
    assert v.manager.submit_job.call_args == (
        ("hashcat", capture, {"hashcat_exe": "/usr/bin/hashcat", "wordlists": ["/lists/a.txt"],
                              "hashcat_args": "-w 3", "default_passwords": True,
                              "ap_ssid_passwords": True, "probed_ssid_passwords": True,
                              "extra_passwords": []}),
        {"auto_crack": True})


def test_save_does_not_submit_to_an_unconfigured_tool(tmp_path, mocker, monkeypatch):
    monkeypatch.setattr(Config, "auto_crack", ToolName.HASHCAT)
    monkeypatch.setattr(Config.hashcat, "path", None)
    v = Vault()
    mocker.patch.object(v.manager, "submit_job")

    v.save_handshake(_ap_with_hs(), _CLIENT)

    v.manager.submit_job.assert_not_called()


def test_save_does_not_submit_a_capture_the_tool_cannot_crack(tmp_path, mocker):
    v = Vault()
    mocker.patch.object(v.manager, "submit_job")
    mocker.patch.object(v.manager.tools[ToolName.CPU_CRACK], "can_crack", return_value=False)

    v.save_handshake(_ap_with_hs(), _CLIENT)

    v.manager.submit_job.assert_not_called()


# ---- PSK verification and related-AP propagation -----------------------------

_STA = "112233445566"


def _write_pmkid_line(d, ssid: str, bssid: str, pmk: bytes) -> None:
    """One PMKID hashline that ``pmk`` cracks, in the AP's aggregate .hc22000."""
    mac_ap = bssid.replace(":", "")
    line = (f"WPA*01*{wpa_psk.pmkid(pmk, bytes.fromhex(mac_ap), bytes.fromhex(_STA)).hex()}"
            f"*{mac_ap}*{_STA}*{ssid.encode().hex()}***\n")
    (d / f"{ssid}_{bssid.replace(':', '-')}.hc22000").write_text(line, encoding="utf-8")


def test_verify_wpa_psk_true_false_none_and_raw_pmk(tmp_path):
    pmk = derive_pmk("correct horse", b"HomeNet")
    _write_pmkid_line(tmp_path, "HomeNet", "aa:bb:cc:dd:ee:01", pmk)
    v = Vault()
    assert v.verify_wpa_psk("aa:bb:cc:dd:ee:01", "correct horse") is True
    assert v.verify_wpa_psk("aa:bb:cc:dd:ee:01", "wrong horse") is False
    assert v.verify_wpa_psk("aa:bb:cc:dd:ee:01", pmk.hex().upper()) is True
    assert v.verify_wpa_psk("aa:bb:cc:dd:ee:99", "correct horse") is None


def _append_eapol_line(d, ssid: str, bssid: str, message_pair: str) -> None:
    """One EAPOL hashline with a MIC no PSK matches, appended to the AP's .hc22000."""
    mac_ap = bssid.replace(":", "")
    line = (f"WPA*02*{'00' * 16}*{mac_ap}*{_STA}*{ssid.encode().hex()}*{'11' * 32}"
            f"*{_eapol_payload(bytes(16)).hex()}*{message_pair}\n")
    with (d / f"{ssid}_{bssid.replace(':', '-')}.hc22000").open("a", encoding="utf-8") as f:
        f.write(line)


def test_verify_wpa_psk_m1m2_lines_cannot_reject(tmp_path):
    _append_eapol_line(tmp_path, "HomeNet", "02:00:00:00:00:01", "00")
    _append_eapol_line(tmp_path, "HomeNet", "02:00:00:00:00:01", "80")
    v = Vault()
    assert v.verify_wpa_psk("02:00:00:00:00:01", "wrong horse") is None
    _append_eapol_line(tmp_path, "HomeNet", "02:00:00:00:00:01", "02")
    v = Vault()
    assert v.verify_wpa_psk("02:00:00:00:00:01", "wrong horse") is False


def test_verify_wpa_psk_matches_beside_m1m2_lines(tmp_path):
    pmk = derive_pmk("correct horse", b"HomeNet")
    _write_pmkid_line(tmp_path, "HomeNet", "02:00:00:00:00:01", pmk)
    _append_eapol_line(tmp_path, "HomeNet", "02:00:00:00:00:01", "00")
    v = Vault()
    assert v.verify_wpa_psk("02:00:00:00:00:01", pmk.hex()) is True
    assert v.verify_wpa_psk("02:00:00:00:00:01", "f" * 64) is False


def test_propagate_saves_only_related_aps_it_verifies_without_a_psk(tmp_path):
    psk = "correct horse"
    _write_pmkid_line(tmp_path, "HomeNet", "aa:bb:cc:dd:ee:01", derive_pmk(psk, b"HomeNet"))
    _write_pmkid_line(tmp_path, "HomeNet_5G", "02:00:00:00:00:10",
                      derive_pmk(psk, b"HomeNet_5G"))           # SSID family
    _write_pmkid_line(tmp_path, "Guest", "aa:bb:cc:dd:ee:03",
                      derive_pmk(psk, b"Guest"))                # BSSID sibling
    _write_pmkid_line(tmp_path, "HomeNet-Ext", "02:00:00:00:00:20",
                      derive_pmk("other pass", b"HomeNet-Ext"))  # related, does not verify
    _write_pmkid_line(tmp_path, "Office", "02:00:00:00:00:30",
                      derive_pmk(psk, b"Office"))               # unrelated
    _write_wpa_psk(tmp_path, "02-00-00-00-00-40", ssid="HomeNet", psk="kept")   # has a PSK
    v = Vault()

    matched = v.propagate_verified_psk("aa:bb:cc:dd:ee:01", "HomeNet", psk)

    assert sorted(matched) == ["02:00:00:00:00:10", "aa:bb:cc:dd:ee:03"]
    assert v.known_psk(AccessPoint(bssid="02:00:00:00:00:10")) == psk
    assert v.known_psk(AccessPoint(bssid="02:00:00:00:00:20")) is None
    assert v.known_psk(AccessPoint(bssid="02:00:00:00:00:30")) is None
    assert v.known_psk(AccessPoint(bssid="02:00:00:00:00:40")) == "kept"


def test_delete_capture_removes_the_meta_of_a_loaded_capture_with_a_real_ssid(tmp_path):
    (tmp_path / "Caf__02-00-00-00-00-40_1700000001_wpa_psk.txt").write_text(
        "SSID: Café\nBSSID: x\nPSK: pw\n", encoding="utf-8")
    meta = update_meta("Café", "02:00:00:00:00:40", nickname="kitchen")
    v = Vault()
    capture = v.persisted("02:00:00:00:00:40")[0]
    assert capture.ssid == "Café" and meta == tmp_path / "Caf__02-00-00-00-00-40.meta"
    v.delete_capture(capture)
    assert not meta.exists()


def test_ssid_prefers_a_capture_holding_the_real_ssid_over_a_newer_pcap(tmp_path):
    (tmp_path / "Caf__02-00-00-00-00-50_1700000001_wpa_psk.txt").write_text(
        "SSID: Café\nBSSID: x\nPSK: pw\n", encoding="utf-8")
    (tmp_path / "Caf__02-00-00-00-00-50_1700000009_handshake.pcap").write_bytes(b"")
    v = Vault()
    assert v.persisted("02:00:00:00:00:50")[0].path.endswith(".pcap")
    assert v.ssid("02:00:00:00:00:50") == "Café"
    assert v.ssid("02:00:00:00:00:99") is None
