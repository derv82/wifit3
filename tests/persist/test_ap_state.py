"""Tests for one AP's keys, hashlines, SSIDs and password status, across its SSID files."""
from __future__ import annotations

import os
import time
from pathlib import Path

from wifit3.crack import wpa_psk
from wifit3.crack.hc22000_format import hashline_id, parse_hc22000
from wifit3.dot11.wpa import derive_pmk
from wifit3.models import CaptureType, PersistedCapture
from wifit3.persist.capture_history import load_capture_index
from wifit3.persist.ap_state import (
    DatedHashline,
    Key,
    LiveVerification,
    PasswordStatus,
    confirmed_at,
    is_confirmed,
    key_statuses,
    load_ap_state,
    password_status,
    record_hashlines_captured,
    record_live_verification,
)
from wifit3.persist.meta import meta_path, read_meta, update_meta
from wifit3.persist.pcap import write_pcap
from tests.hashline_dates import write_hashline_dates

# hashcat wiki example_hashes, mode 22000 (password "hashcat!"); message pair a2 = M2+M3.
_BSSID = "64:66:b3:8e:c3:fc"
_DASHED = "64-66-b3-8e-c3-fc"
_MIC = "024022795224bffca545276c3762686f"
_EAPOL_LINE = (
    f"WPA*02*{_MIC}*6466b38ec3fc*225edc49b7aa"
    "*54502d4c494e4b5f484153484341545f54455354"
    "*10e3be3b005a629e89de088d6a2fdc489db83ad4764f2d186b9cde15446e972e"
    "*0103007502010a0000000000000000000148ce2ccba9c1fda130ff2fbbfb4fd3b063d1a93920b0f7df54a5cb"
    "f787b16171000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000001630140100000fac040100000fac040100000fac028000*a2")
_CHALLENGE_LINE = _EAPOL_LINE[:-2] + "80"       # same MIC, flagged M1+M2
_OTHER_LINE = _EAPOL_LINE.replace(_MIC, "f" * 32)
_SSID = "TP-LINK_HASHCAT_TEST"
_OLD_SSID_LINE = _OTHER_LINE.replace("54502d4c494e4b5f484153484341545f54455354", b"Old".hex())


def _hashline(raw: str, *captured: int) -> DatedHashline:
    return DatedHashline(hashline_id(raw), raw, parse_hc22000(raw), captured)


def _capture(capture_type: CaptureType, saved_at: int, value: str = "hashcat!", ssid: str = _SSID) -> PersistedCapture:
    return PersistedCapture(type=capture_type, timestamp=saved_at, path=f"{saved_at}.txt", bssid=_BSSID,
                            value=value, ssid=ssid)


def _key(*hashlines: DatedHashline, value: str = "hashcat!", key_type: CaptureType = CaptureType.WPA_PSK,
         capture_type: CaptureType = CaptureType.WPA_PSK, saved_at: int = 1,
         verifications: tuple[LiveVerification, ...] = ()) -> Key:
    return Key(key_type, value, (_capture(capture_type, saved_at, value),),
               frozenset(h.id for h in hashlines), verifications)


def test_no_key_is_not_cracked():
    assert password_status([_hashline(_EAPOL_LINE, 10)], []) == (PasswordStatus.NOT_CRACKED, None)


def test_only_a_wps_pin_is_not_cracked():
    pin = _key(value="12345670", key_type=CaptureType.WPS_PIN, capture_type=CaptureType.WPS_PIN)
    assert password_status([], [pin]) == (PasswordStatus.NOT_CRACKED, None)


def test_a_key_matching_the_newest_authorized_hashline_is_current():
    newest = _hashline(_EAPOL_LINE, 20)
    key = _key(newest)
    assert password_status([newest, _hashline(_OTHER_LINE, 10)], [key]) == (PasswordStatus.CURRENT, key)


def test_a_newer_authorized_hashline_no_key_matches_is_stale():
    key = _key(old := _hashline(_EAPOL_LINE, 10))
    assert password_status([_hashline(_OTHER_LINE, 20), old], [key]) == (PasswordStatus.STALE, key)


def test_stale_names_the_key_that_matched_last_not_the_first_listed():
    matched = _key(old := _hashline(_EAPOL_LINE, 10))
    unmatched_older = _key(value="guess", saved_at=5)
    status = password_status([_hashline(_OTHER_LINE, 20), old], [unmatched_older, matched])
    assert status == (PasswordStatus.STALE, matched)


def test_a_key_cracked_late_from_an_old_capture_is_still_stale():
    late = _key(old := _hashline(_EAPOL_LINE, 10), saved_at=30)
    assert password_status([_hashline(_OTHER_LINE, 20), old], [late]) == (PasswordStatus.STALE, late)


def test_stale_with_no_key_ever_matching_names_no_key():
    guess = _key(value="guess", saved_at=5)
    assert password_status([_hashline(_OTHER_LINE, 20)], [guess]) == (PasswordStatus.STALE, None)


def test_a_key_cracked_from_a_newer_m1m2_line_is_refuted_by_the_older_authorized_one():
    newer = _key(challenge := _hashline(_CHALLENGE_LINE, 25), value="newpass", saved_at=30)
    status = password_status([challenge, _hashline(_OTHER_LINE, 20)], [newer])
    assert status == (PasswordStatus.STALE, None)


def test_a_key_the_ap_gave_up_before_the_unmatched_capture_is_stale_though_saved_again_after():
    key = Key(CaptureType.WPA_PSK, "hashcat!", (_capture(CaptureType.WPA_PSK, 30), _capture(CaptureType.WPS_PBC, 10)),
              frozenset(), ())
    assert password_status([_hashline(_OTHER_LINE, 20)], [key]) == (PasswordStatus.STALE, key)


def test_a_key_saved_after_the_unmatched_capture_is_refuted_by_it():
    old_key = _key(old := _hashline(_EAPOL_LINE, 10))
    added = _key(value="newpass", saved_at=30)
    status = password_status([_hashline(_OTHER_LINE, 20), old], [added, old_key])
    assert status == (PasswordStatus.STALE, old_key)


def test_a_newer_challenge_hashline_does_not_make_it_stale():
    key = _key(old := _hashline(_EAPOL_LINE, 10))
    status, _ = password_status([_hashline(_OTHER_LINE[:-2] + "80", 20), old], [key])
    assert status is PasswordStatus.CURRENT


def test_only_challenge_matches_is_not_verified():
    key = _key(challenge := _hashline(_CHALLENGE_LINE, 10))
    assert password_status([challenge], [key]) == (PasswordStatus.NOT_VERIFIED, key)


def test_a_correct_live_verification_after_a_change_makes_the_new_key_current():
    old_key = _key(old := _hashline(_EAPOL_LINE, 10))
    new_key = _key(value="newpass", saved_at=25, verifications=(LiveVerification(30, True),))
    status = password_status([_hashline(_OTHER_LINE, 20), old], [new_key, old_key])
    assert status == (PasswordStatus.CURRENT, new_key)


def test_a_live_rejection_newer_than_the_proof_makes_the_key_stale():
    key = _key(newest := _hashline(_EAPOL_LINE, 10), verifications=(LiveVerification(20, False),))
    assert password_status([newest], [key]) == (PasswordStatus.STALE, key)


def test_a_key_recovered_from_the_ap_is_current_without_hashlines():
    wep = _key(value="6162636465", key_type=CaptureType.WEP, capture_type=CaptureType.WEP)
    assert password_status([], [wep]) == (PasswordStatus.CURRENT, wep)


def test_a_rejected_wps_pin_does_not_touch_the_psk():
    psk = _key(capture_type=CaptureType.WPS_PIN, saved_at=10)
    pin = _key(value="12345670", key_type=CaptureType.WPS_PIN, capture_type=CaptureType.WPS_PIN,
               saved_at=10, verifications=(LiveVerification(20, False),))
    assert password_status([], [pin, psk]) == (PasswordStatus.CURRENT, psk)


def test_undated_authorized_evidence_is_older_than_any_dated():
    key = _key(dated := _hashline(_EAPOL_LINE, 10))
    assert password_status([dated, _hashline(_OTHER_LINE)], [key]) == (PasswordStatus.CURRENT, key)


def test_a_key_matched_only_by_an_undated_authorized_hashline_is_confirmed_but_undated():
    key = _key(undated := _hashline(_EAPOL_LINE))
    assert password_status([undated], [key]) == (PasswordStatus.CURRENT, key)
    assert is_confirmed(key, [undated]) and confirmed_at(key, [undated]) is None


def test_an_m1m2_match_does_not_confirm():
    key = _key(challenge := _hashline(_CHALLENGE_LINE, 10))
    assert not is_confirmed(key, [challenge]) and confirmed_at(key, [challenge]) is None


def _key_statuses(hashlines: list[DatedHashline], keys: list[Key]) -> dict[tuple[CaptureType, str], PasswordStatus]:
    return key_statuses(keys, hashlines, *password_status(hashlines, keys))


def test_a_key_newer_evidence_refutes_is_stale_beside_the_current_one():
    old, new = _hashline(_OTHER_LINE, 10), _hashline(_EAPOL_LINE, 20)
    current, replaced = _key(new), _key(old, value="old-password")
    assert _key_statuses([new, old], [current, replaced]) == {
        current.id: PasswordStatus.CURRENT, replaced.id: PasswordStatus.STALE}


def test_with_only_undated_evidence_the_status_key_alone_is_current():
    first, second = _hashline(_EAPOL_LINE), _hashline(_OTHER_LINE)
    one, other = _key(first), _key(second, value="other-password")
    status, status_key = password_status([first, second], [one, other])
    statuses = key_statuses([one, other], [first, second], status, status_key)
    assert statuses[status_key.id] is PasswordStatus.CURRENT
    assert sorted(s.name for s in statuses.values()) == ["CURRENT", "STALE"]


def test_a_key_nothing_confirms_is_not_verified_and_a_pin_from_the_ap_is_current():
    newest = _hashline(_EAPOL_LINE, 20)
    current, guess = _key(newest), _key(value="guess")
    pin = _key(value="12345670", key_type=CaptureType.WPS_PIN, capture_type=CaptureType.WPS_PIN)
    assert _key_statuses([newest], [current, guess, pin]) == {
        current.id: PasswordStatus.CURRENT, guess.id: PasswordStatus.NOT_VERIFIED, pin.id: PasswordStatus.CURRENT}


def test_a_loaded_state_carries_each_keys_status(tmp_path):
    _write_aggregate(_SSID, _EAPOL_LINE)
    _write_psk(_SSID, 100, "hashcat!")
    _write_psk(_SSID, 200, "guess")
    state = _state()
    assert {k.value: state.key_statuses[k.id] for k in state.keys} == {
        "hashcat!": PasswordStatus.CURRENT, "guess": PasswordStatus.NOT_VERIFIED}


def _write_aggregate(ssid: str, *lines: str) -> Path:
    from wifit3.persist.config import Config
    path = Path(Config.captures_dir) / f"{ssid}_{_DASHED}.hc22000"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_psk(ssid: str, epoch: int, psk: str) -> None:
    from wifit3.persist.config import Config
    (Path(Config.captures_dir) / f"{ssid}_{_DASHED}_{epoch}_wpa_psk.txt").write_text(
        f"SSID: {ssid}\nBSSID: {_BSSID}\nPSK: {psk}\n", encoding="utf-8")


def _state():
    return load_ap_state(_BSSID, load_capture_index()[_BSSID])


def test_load_matches_saved_psks_against_every_ssid_file(tmp_path):
    _write_aggregate("Old", _OLD_SSID_LINE)
    _write_aggregate(_SSID, _EAPOL_LINE)
    _write_psk(_SSID, 1700000000, "hashcat!")
    write_hashline_dates("Old", _BSSID, {_OLD_SSID_LINE: 1600000000})
    write_hashline_dates(_SSID, _BSSID, {_EAPOL_LINE: 1700000000})

    state = _state()
    assert state.status is PasswordStatus.CURRENT
    assert state.status_key.value == "hashcat!"
    assert [h.raw for h in state.uncracked] == [_OLD_SSID_LINE]
    assert [(s.ssid, s.first_seen) for s in state.ssids] == [(_SSID, 1700000000), ("Old", 1600000000)]
    assert state.hashline_capture.path.endswith(f"{_SSID}_{_DASHED}.hc22000")


def test_jobs_are_filed_under_the_newest_ssids_file_even_when_its_ssid_is_not_utf8(tmp_path):
    non_utf8_line = _OTHER_LINE.replace("54502d4c494e4b5f484153484341545f54455354", b"Caf\xe9".hex())
    newest = _write_aggregate("Caf_", non_utf8_line)
    older = _write_aggregate("Old", _OLD_SSID_LINE)
    os.utime(newest, (1_600_000_000, 1_600_000_000))       # the older file on disk
    os.utime(older, (1_700_000_000, 1_700_000_000))
    write_hashline_dates("Caf_", _BSSID, {non_utf8_line: 1_700_000_000})
    write_hashline_dates("Old", _BSSID, {_OLD_SSID_LINE: 1_600_000_000})

    state = _state()
    assert state.ssids[0].ssid == "Caf�"
    assert state.hashline_capture.path == str(newest)


def test_load_lists_undated_hashlines_last(tmp_path):
    _write_aggregate(_SSID, _OTHER_LINE, _EAPOL_LINE)
    write_hashline_dates(_SSID, _BSSID, {_EAPOL_LINE: 1700000000})
    assert [(h.raw, h.first_captured) for h in _state().hashlines] == [
        (_EAPOL_LINE, 1700000000), (_OTHER_LINE, None)]


def test_an_undated_authorized_match_beside_a_dated_m1m2_line_is_current(tmp_path):
    unmatched_challenge = _OTHER_LINE[:-2] + "80"
    _write_aggregate(_SSID, _EAPOL_LINE, unmatched_challenge)
    _write_psk(_SSID, 1700000000, "hashcat!")
    write_hashline_dates(_SSID, _BSSID, {unmatched_challenge: 1700000000})
    state = _state()
    assert [h.raw for h in state.hashlines] == [unmatched_challenge, _EAPOL_LINE]
    assert (state.status, state.status_key.value) == (PasswordStatus.CURRENT, "hashcat!")


def test_two_ssids_with_different_psks_each_match_their_own_lines(tmp_path):
    sta = bytes.fromhex("225edc49b7aa")
    old_pmkid = wpa_psk.pmkid(derive_pmk("oldpass1", b"Old"), bytes.fromhex(_BSSID.replace(":", "")), sta)
    old_line = f"WPA*01*{old_pmkid.hex()}*{_BSSID.replace(':', '')}*{sta.hex()}*{b'Old'.hex()}***"
    _write_aggregate("Old", old_line)
    _write_aggregate(_SSID, _EAPOL_LINE)
    _write_psk("Old", 1600000001, "oldpass1")
    _write_psk(_SSID, 1700000001, "hashcat!")
    write_hashline_dates("Old", _BSSID, {old_line: 1600000000})
    write_hashline_dates(_SSID, _BSSID, {_EAPOL_LINE: 1700000000})
    state = _state()
    assert {k.value: k.hashline_ids for k in state.keys} == {
        "hashcat!": {hashline_id(_EAPOL_LINE)}, "oldpass1": {hashline_id(old_line)}}
    assert (state.status, state.status_key.value, state.uncracked) == (PasswordStatus.CURRENT, "hashcat!", ())


def test_a_wps_pbc_capture_is_a_current_psk(tmp_path):
    state = load_ap_state(_BSSID, [_capture(CaptureType.WPS_PBC, 1)])
    assert [(k.type, k.value) for k in state.keys] == [(CaptureType.WPA_PSK, "hashcat!")]
    assert (state.status, state.status_key) == (PasswordStatus.CURRENT, state.keys[0])


def test_load_dates_a_hashline_from_the_pcap_frame_carrying_its_mic(tmp_path):
    _write_aggregate(_SSID, _EAPOL_LINE)
    write_pcap(tmp_path / f"{_SSID}_{_DASHED}_1700000005_handshake.pcap",
               [(b"\x88\x02" + bytes.fromhex(_MIC), 1700000003.5)])
    assert _state().hashlines[0].captured == (1700000003,)


def test_record_hashlines_captured_dates_them_now_and_replaces_a_bad_record(tmp_path):
    _write_aggregate(_SSID, _EAPOL_LINE, _OTHER_LINE)
    update_meta(_SSID, _BSSID, hashlines={hashline_id(_EAPOL_LINE): "not a record"})
    before = int(time.time())
    record_hashlines_captured(_SSID, _BSSID, [_EAPOL_LINE, _OTHER_LINE])
    assert all(before <= h.first_captured <= int(time.time()) for h in _state().hashlines)


def test_a_recorded_live_verification_is_read_back(tmp_path):
    _write_aggregate(_SSID, _CHALLENGE_LINE)
    _write_psk(_SSID, 1700000000, "hashcat!")
    assert _state().status is PasswordStatus.NOT_VERIFIED
    record_live_verification(_state().keys[0], correct=True)
    state = _state()
    assert state.status is PasswordStatus.CURRENT
    assert [v.correct for v in state.keys[0].live_verifications] == [True]


def test_a_live_verification_of_an_unknown_kind_is_ignored(tmp_path):
    _write_psk(_SSID, 1700000000, "hashcat!")
    update_meta(_SSID, _BSSID, live_verifications=[
        {"kind": "PSK", "key": "hashcat!", "at": 1700000001, "correct": True},
        {"kind": "WPA_PSK", "key": "hashcat!", "at": 1700000002, "correct": True}])
    assert [v.at for v in _state().keys[0].live_verifications] == [1700000002]


def test_a_wps_pin_capture_gives_a_current_psk_key_and_a_pin_key(tmp_path):
    capture = PersistedCapture(type=CaptureType.WPS_PIN, timestamp=1, path="x.txt", bssid=_BSSID,
                               value="hashcat!", pin="12345670", ssid=_SSID)
    state = load_ap_state(_BSSID, [capture])
    assert sorted((k.type, k.value) for k in state.keys) == [
        (CaptureType.WPA_PSK, "hashcat!"), (CaptureType.WPS_PIN, "12345670")]
    assert (state.status, state.status_key.type) == (PasswordStatus.CURRENT, CaptureType.WPA_PSK)


def test_a_psk_saved_in_two_files_keeps_both_captures(tmp_path):
    wps, cracked = _capture(CaptureType.WPS_PBC, 1), _capture(CaptureType.WPA_PSK, 2)
    [key] = load_ap_state(_BSSID, [wps, cracked]).keys
    assert key.captures == (cracked, wps)
    assert key.ap_recovered_at == 1


def test_a_live_verification_is_recorded_in_the_newest_captures_ssid(tmp_path):
    key = Key(CaptureType.WPA_PSK, "hashcat!", (_capture(CaptureType.WPA_PSK, 2, ssid="New"),
                                                _capture(CaptureType.WPA_PSK, 1, ssid="Old")), frozenset(), ())
    record_live_verification(key, correct=True)
    assert len(read_meta(meta_path("New", _BSSID))["live_verifications"]) == 1
    assert "live_verifications" not in read_meta(meta_path("Old", _BSSID))
