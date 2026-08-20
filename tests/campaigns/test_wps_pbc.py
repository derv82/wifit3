"""Tests for the PBC window-edge watcher + credential save, and capture()'s lease."""

from types import SimpleNamespace

import pytest

from wifit3.campaigns import pbc as pbc_mod
from wifit3.campaigns.pbc import PbcWatcher, WpsPbcCapture
from wifit3.campaigns.wps.registrar import AttemptOutcome, PinResult
from wifit3.wlan.lease import Lease
from wifit3.dot11 import mac_to_str, str_to_mac


def _ap(bssid, active):
    return SimpleNamespace(bssid=bssid, wps_pbc_active=active)


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




# ----- capture() arms/restores through the lease (migration guard) -----------

_ARMED = "02:11:22:33:44:55"


class _FakeIface:
    """Records the arm/disarm calls the lease drives, plus any injected frame."""

    def __init__(self):
        self.current_channel = 1
        self.calls: list = []
        self.sent: list = []

    async def set_channel(self, ch, scan=False):
        self.current_channel = ch
        self.calls.append(("set_channel", ch))
        return True

    async def set_fake_mac(self, mac, bssid=None):
        self.calls.append(("set_fake_mac", mac, bssid))
        return _ARMED

    async def clear_fake_mac(self):
        self.calls.append(("clear_fake_mac",))

    async def enable_rx_acks(self):
        self.calls.append(("enable_rx_acks",))

    async def disable_rx_acks(self):
        self.calls.append(("disable_rx_acks",))

    def active_monitor_warning(self):
        return None

    async def send_no_wait(self, frame):
        self.sent.append(frame)
        return True


class _FakeArray:
    def __init__(self, iface):
        self._iface = iface
        self.registered: list = []
        self.unregistered: list = []

    def select_iface(self, channel):
        return self._iface

    def register_own_mac(self, mac):
        s = mac_to_str(mac) if isinstance(mac, (bytes, bytearray)) else str(mac).lower()
        self.registered.append(s)
        return s

    def unregister_own_mac(self, mac):
        self.unregistered.append(mac)

    def lease(self, channel=None, fake_mac=None, bssid=None, ack_tally=False, iface=None):
        return Lease(self, iface or self._iface, channel=channel, fake_mac=fake_mac,
                     bssid=bssid, ack_tally=ack_tally)


class _FakeAssoc:
    def __init__(self, *a, **k):
        self.fail_reason = None
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    async def associate(self, attempts=5):
        return True


class _FakeTransport:
    def __init__(self, *a, **k):
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def _fake_enrollee_cls(outcome):
    class _E:
        def __init__(self, *a, **k):
            pass

        async def run(self):
            return outcome
    return _E


def _pbc_target():
    return SimpleNamespace(bssid="34:21:09:00:01:ff", ssid="TESTPBC", channel=1)


def _patch_collaborators(monkeypatch, outcome):
    monkeypatch.setattr(pbc_mod, "Association", _FakeAssoc)
    monkeypatch.setattr(pbc_mod, "WlanTransport", _FakeTransport)
    monkeypatch.setattr(pbc_mod, "WpsEnrollee", _fake_enrollee_cls(outcome))


@pytest.mark.asyncio
async def test_pbc_capture_arms_and_restores_via_lease(monkeypatch):
    """capture() arms active-monitor + own-MAC + ACK tally through the lease and clears
    all three on exit; a non-SUCCESS outcome sends a client-leaving deauth first, while
    the lease is still armed."""
    iface = _FakeIface()
    array = _FakeArray(iface)
    aborted = AttemptOutcome(PinResult.ABORTED, "<PBC>", detail="x")
    _patch_collaborators(monkeypatch, aborted)

    camp = WpsPbcCapture(array, _pbc_target(), our_mac=bytes.fromhex("02aabbccddee"))
    outcome = await camp.capture()

    assert outcome is aborted
    names = [c[0] for c in iface.calls]
    assert names.index("set_fake_mac") < names.index("enable_rx_acks")     # armed on enter
    assert "disable_rx_acks" in names and "clear_fake_mac" in names        # cleared on exit
    assert array.registered == [_ARMED] and array.unregistered == [_ARMED]  # own reg then unreg
    assert camp.our_mac == str_to_mac(_ARMED)                              # adopted the armed MAC
    assert len(iface.sent) == 1                                            # leaving-deauth (non-success)


@pytest.mark.asyncio
async def test_pbc_capture_success_skips_leaving_deauth(monkeypatch):
    """A SUCCESS outcome keeps the session (no leaving-deauth), but the lease still
    disarms the card on exit."""
    iface = _FakeIface()
    array = _FakeArray(iface)
    ok = AttemptOutcome(PinResult.SUCCESS, "<PBC>", psk="secret")
    _patch_collaborators(monkeypatch, ok)

    camp = WpsPbcCapture(array, _pbc_target(), our_mac=bytes.fromhex("02aabbccddee"))
    outcome = await camp.capture()

    assert outcome is ok
    assert iface.sent == []                                                # success → no leaving-deauth
    assert "clear_fake_mac" in [c[0] for c in iface.calls]                 # still disarmed on exit
