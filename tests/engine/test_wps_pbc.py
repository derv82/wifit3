"""Tests for the PBC arming mode + window-edge watcher + credential save."""

from types import SimpleNamespace

from wifit3.engine.attacks.wps.pbc import PbcArmMode, PbcWatcher, save_pbc_credential


def _ap(bssid, active):
    return SimpleNamespace(bssid=bssid, wps_pbc_active=active)


def test_arm_mode_cycles():
    m = PbcArmMode.OFF
    m = m.cycled(); assert m is PbcArmMode.SELECTED
    m = m.cycled(); assert m is PbcArmMode.GLOBAL
    m = m.cycled(); assert m is PbcArmMode.OFF


def test_watcher_edge_triggers_once_per_window():
    w = PbcWatcher()
    a, b = _ap("aa", False), _ap("bb", False)

    assert w.new_windows([a, b]) == []          # nothing active

    a.wps_pbc_active = True
    opened = w.new_windows([a, b])
    assert [x.bssid for x in opened] == ["aa"]   # rising edge

    assert w.new_windows([a, b]) == []           # still open → no re-trigger

    b.wps_pbc_active = True
    assert [x.bssid for x in w.new_windows([a, b])] == ["bb"]


def test_watcher_reopen_retriggers():
    w = PbcWatcher()
    a = _ap("aa", True)
    assert [x.bssid for x in w.new_windows([a])] == ["aa"]
    a.wps_pbc_active = False
    assert w.new_windows([a]) == []              # window closed
    a.wps_pbc_active = True
    assert [x.bssid for x in w.new_windows([a])] == ["aa"]   # re-opened → fires again


def test_save_pbc_credential(tmp_path):
    path = save_pbc_credential("HomeNet", "aa:bb:cc:dd:ee:05", "yxws3tik", captures_dir=str(tmp_path))
    assert path.exists() and path.suffix == ".wps"
    body = path.read_text()
    assert "SSID: HomeNet" in body and "PSK: yxws3tik" in body and "WPS-PBC" in body


def test_save_pbc_credential_sanitizes_ssid(tmp_path):
    path = save_pbc_credential("../evil name/", "aa:bb:cc:dd:ee:ff", "pw", captures_dir=str(tmp_path))
    assert path.parent == tmp_path                # no path escape
    assert "/" not in path.name and "\\" not in path.name
