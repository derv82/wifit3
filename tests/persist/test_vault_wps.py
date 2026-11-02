"""WPS PBC / PIN recoveries followed by captures, end to end through the Vault's save paths."""
from __future__ import annotations

from pathlib import Path

from wifit3.crack import wpa_psk
from wifit3.dot11.wpa import derive_pmk
from wifit3.models import AccessPoint, CaptureType, Handshake, ToolName
from wifit3.persist.ap_state import ApState, PasswordStatus
from wifit3.persist.config import Config
from wifit3.persist.vault import HashlineSaveResult, Vault
from tests.hashline_dates import write_hashline_dates

_BSSID = "aa:bb:cc:dd:ee:ff"
_SSID = "HomeNet"
_PSK = "hunter22"


def _pmkid_ap(psk: str, client_mac: str, ssid: str = _SSID) -> AccessPoint:
    """An AP holding a PMKID that ``psk`` cracks: authorized evidence."""
    ap = AccessPoint(bssid=_BSSID, ssid=ssid)
    pmkid = wpa_psk.pmkid(derive_pmk(psk, ssid.encode()), bytes.fromhex(_BSSID.replace(":", "")),
                          bytes.fromhex(client_mac.replace(":", "")))
    ap.handshakes[client_mac] = Handshake(bssid=_BSSID, client_mac=client_mac, beacon_frame=b"BEACON",
                                          pmkid=pmkid)
    return ap


def _save_pmkid(vault: Vault, psk: str, client_mac: str, ssid: str = _SSID) -> HashlineSaveResult:
    result = vault.save_pmkid(_pmkid_ap(psk, client_mac, ssid), client_mac)
    assert result is not None and result.was_new
    return result


def _write_pbc_from_an_earlier_session(captures_dir: Path, epoch: int) -> None:
    (captures_dir / f"{_SSID}_{_BSSID.replace(':', '-')}_{epoch}_wps_pbc.txt").write_text(
        f"SSID: {_SSID}\nBSSID: {_BSSID}\nPSK: {_PSK}\n", encoding="utf-8")


def _status(state: ApState) -> tuple[PasswordStatus, CaptureType | None, str | None]:
    key = state.status_key
    return state.status, key.type if key else None, key.value if key else None


def _later_session_ap() -> AccessPoint:
    """The AP as a later session sees it: no WPS result held in memory."""
    return AccessPoint(bssid=_BSSID, ssid=_SSID)


def test_a_pbc_psk_is_the_current_password(tmp_path):
    vault = Vault()
    ap = AccessPoint(bssid=_BSSID, ssid=_SSID)
    ap.wps_pbc_psk = _PSK
    assert vault.save_wps_pbc(ap, _PSK).was_new

    state = vault.ap_state(_BSSID)
    [key] = state.keys
    assert (key.type, key.value, key.ap_recovered_at is not None) == (CaptureType.WPA_PSK, _PSK, True)
    assert _status(state) == (PasswordStatus.CURRENT, CaptureType.WPA_PSK, _PSK)
    assert vault.known_psk(_later_session_ap()) == _PSK and vault.has_psk(_later_session_ap())


def test_a_capture_the_pbc_psk_cracks_keeps_it_current_and_auto_cracks_nothing(tmp_path):
    vault = Vault()
    vault.save_wps_pbc(_later_session_ap(), _PSK)

    assert _save_pmkid(vault, _PSK, "11:22:33:44:55:01").matches_saved_psk is True

    assert _status(vault.ap_state(_BSSID)) == (PasswordStatus.CURRENT, CaptureType.WPA_PSK, _PSK)
    assert vault.ap_state(_BSSID).uncracked == ()
    assert vault.manager.jobs == {}


def test_a_capture_refuting_a_current_pbc_psk_makes_it_stale_and_auto_cracks_once(tmp_path):
    assert Config.auto_crack is ToolName.CPU_CRACK
    _write_pbc_from_an_earlier_session(tmp_path, 1_700_000_000)
    vault = Vault()
    _save_pmkid(vault, _PSK, "11:22:33:44:55:01")
    write_hashline_dates(_SSID, _BSSID, {vault.ap_state(_BSSID).hashlines[0].raw: 1_700_000_100})
    vault.notify_changed(_BSSID)
    assert vault.ap_state(_BSSID).status is PasswordStatus.CURRENT and vault.manager.jobs == {}

    assert _save_pmkid(vault, "changedpw", "11:22:33:44:55:02").matches_saved_psk is False

    state = vault.ap_state(_BSSID)
    assert _status(state) == (PasswordStatus.STALE, CaptureType.WPA_PSK, _PSK)
    assert vault.known_psk(_later_session_ap()) is None
    [job] = vault.manager.jobs.values()
    assert job.auto_crack and job.tool_name == ToolName.CPU_CRACK
    assert Path(job.hashlines_path).read_text(encoding="utf-8").split() == [h.raw for h in state.uncracked]

    assert _save_pmkid(vault, "changedpw", "11:22:33:44:55:03").matches_saved_psk is False
    assert len(vault.manager.jobs) == 1


def test_a_wps_pin_recovery_shows_its_psk_and_a_rejected_pin_check_changes_nothing(tmp_path):
    vault = Vault()
    vault.save_wps_pin(_later_session_ap(), "12345670", _PSK)
    state = vault.ap_state(_BSSID)
    assert sorted((k.type, k.value) for k in state.keys) == [
        (CaptureType.WPA_PSK, _PSK), (CaptureType.WPS_PIN, "12345670")]
    assert _status(state) == (PasswordStatus.CURRENT, CaptureType.WPA_PSK, _PSK)

    [pin_key] = [k for k in state.keys if k.type == CaptureType.WPS_PIN]
    vault.record_live_verification(pin_key, correct=False)

    state = vault.ap_state(_BSSID)
    assert _status(state) == (PasswordStatus.CURRENT, CaptureType.WPA_PSK, _PSK)
    assert [v.correct for k in state.keys if k.type == CaptureType.WPS_PIN for v in k.live_verifications] == [False]


def test_a_pbc_psk_stays_current_after_the_ap_is_renamed(tmp_path):
    vault = Vault()
    vault.save_wps_pbc(_later_session_ap(), _PSK)

    assert _save_pmkid(vault, _PSK, "11:22:33:44:55:01", ssid="HomeNet_Renamed").matches_saved_psk is True

    state = vault.ap_state(_BSSID)
    assert _status(state) == (PasswordStatus.CURRENT, CaptureType.WPA_PSK, _PSK)
    assert {s.ssid for s in state.ssids} == {_SSID, "HomeNet_Renamed"}
    assert vault.manager.jobs == {}
