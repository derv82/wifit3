"""Campaign sweep logic, driven by a scripted oracle (no hardware).

The campaign's _try() is overridden to simulate an AP with a known PIN, so we
exercise the COMMON→first-half→second-half progression, the first-half-confirmed
switch, success/PSK capture, and .run resume — without a radio or fake enrollee.
"""

from types import SimpleNamespace

from wifit3.engine.attacks.wps import pins
from wifit3.engine.attacks.wps.campaign import CampaignState, WpsCampaign, _state_path
from wifit3.engine.attacks.wps.registrar import AttemptOutcome, PinResult
from wifit3.engine.attacks.wps.wsc_crypto import pin_is_valid


def _target(bssid="aa:bb:cc:dd:ee:ff", ssid="Net", ch=1):
    return SimpleNamespace(bssid=bssid, ssid=ssid, channel=ch, wps_locked=False)


def _iface():
    return SimpleNamespace(access_points={})


class ScriptedCampaign(WpsCampaign):
    """Campaign whose _try simulates a real AP holding ``known_pin`` + ``psk``."""

    def __init__(self, *a, known_pin, psk, **kw):
        super().__init__(*a, **kw)
        self.known_pin = known_pin
        self.psk = psk
        self.tried = []

    async def _try(self, pin):
        self.tried.append(pin)
        f, s = pins.split_pin(pin)
        if f != self.known_pin[:4]:
            return AttemptOutcome(PinResult.FIRST_HALF_WRONG, pin)
        if pin == self.known_pin:
            return AttemptOutcome(PinResult.SUCCESS, pin, psk=self.psk, ssid="Net")
        return AttemptOutcome(PinResult.SECOND_HALF_WRONG, pin)   # first half ok


async def test_campaign_finds_pin_via_full_sweep(tmp_path):
    # A valid PIN NOT in COMMON_PINS so the sweep actually runs.
    known = pins.full_pin("1357", "246")
    assert pin_is_valid(known) and known not in pins.COMMON_PINS

    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=known, psk="hunter2pw")
    await c._run()

    assert c.status == "found"
    assert c.state.found_pin == known
    assert c.state.found_psk == "hunter2pw"
    assert c.state.first_half == "1357"
    # Sweep efficiency: ≤ 8 common + 10000 first-half + 1000 second-half.
    assert len(c.tried) <= len(pins.COMMON_PINS) + 10000 + 1000


async def test_campaign_finds_common_pin_fast(tmp_path):
    known = "12345670"                       # in COMMON_PINS
    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=known, psk="pw")
    await c._run()
    assert c.state.found_pin == known
    assert len(c.tried) == 1                 # found on the first common attempt


async def test_first_half_confirmed_switches_phase(tmp_path):
    known = pins.full_pin("2468", "135")
    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=known, psk="x")
    await c._run()
    assert c.state.found_pin == known
    # Once "2468" matched, it must have pinned the first half and swept halves.
    assert c.state.first_half == "2468"


async def test_run_state_persisted_and_resumed(tmp_path):
    known = pins.full_pin("1357", "246")
    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=known, psk="pw")
    await c._run()

    path = _state_path(str(tmp_path), "aa:bb:cc:dd:ee:ff")
    assert path.exists()
    import json
    data = json.loads(path.read_text())
    assert data["found_pin"] == known and data["phase"] == "done"

    # A fresh campaign loads the prior state.
    c2 = WpsCampaign(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    assert c2.state.found_pin == known


def test_lock_backoff_grows_with_observation():
    from wifit3.engine.attacks.wps.lock import LockTracker
    lt = LockTracker(min_wait=30, max_wait=360, initial_wait=60)
    assert lt.backoff() == 60                 # no observations yet
    lt.begin_lock()
    lt._observed_durations.append(120.0)
    lt.end_lock()
    assert 130 <= lt.backoff() <= 140         # learned ~ max*1.1, clamped
