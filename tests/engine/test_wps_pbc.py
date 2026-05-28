"""Tests for the PBC arming mode + window-edge watcher + credential save."""

from types import SimpleNamespace

from wifit3.engine.attacks.wps.pbc import PbcArmMode, PbcWatcher


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


