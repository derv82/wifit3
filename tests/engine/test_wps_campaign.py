"""Campaign sweep logic, driven by a scripted oracle (no hardware).

The campaign's _try() is overridden to simulate an AP with a known PIN, so we
exercise the COMMON→first-half→second-half progression, the first-half-confirmed
switch, success/PSK capture, and .run resume — without a radio or fake enrollee.
"""

from types import SimpleNamespace

from wifit3.engine.attacks.wps import pins
from wifit3.engine.attacks.wps.campaign import WpsCampaign, _state_path
from wifit3.engine.attacks.wps.registrar import AttemptOutcome, PinResult
from wifit3.engine.attacks.wps.wsc_crypto import pin_is_valid


def _target(bssid="aa:bb:cc:dd:ee:ff", ssid="Net", ch=1):
    return SimpleNamespace(bssid=bssid, ssid=ssid, channel=ch, wps_locked=False)


async def _set_fake_mac(*_a, **_k):
    return None   # un-ACked path: campaign falls back to use_no_ack, as before active-monitor


async def _clear_fake_mac(*_a, **_k):
    return None


def _iface():
    return SimpleNamespace(access_points={},
                           set_fake_mac=_set_fake_mac, clear_fake_mac=_clear_fake_mac)


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
    await c._loop()

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
    await c._loop()
    assert c.state.found_pin == known
    assert len(c.tried) == 1                 # found on the first common attempt


def _write_done_state(tmp_path, found_pin, found_psk, bssid="aa:bb:cc:dd:ee:ff"):
    """Write a .run state file mimicking a previously-successful campaign."""
    import json
    p = tmp_path / f"wps_{bssid.replace(':', '-')}.run"
    p.write_text(json.dumps({
        "bssid": bssid, "phase": "done",
        "found_pin": found_pin, "found_psk": found_psk,
    }))


async def test_resume_verifies_pin_psk_unchanged(tmp_path):
    # Re-running on an AP whose PIN + PSK are unchanged: verify confirms it,
    # nothing is reset, found_psk stays put.
    known = pins.full_pin("1357", "246")
    _write_done_state(tmp_path, known, "originalpsk")
    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=known, psk="originalpsk")
    await c._loop()
    assert c.state.phase == "done"
    assert c.state.found_pin == known
    assert c.state.found_psk == "originalpsk"
    assert c.tried == [known]                  # exactly one verify attempt


async def test_resume_catches_psk_rotation(tmp_path):
    # PIN unchanged but the AP's password was rotated — verify picks up the
    # NEW PSK from the recovered exchange. The high-value scenario.
    known = pins.full_pin("1357", "246")
    _write_done_state(tmp_path, known, "oldpassword")
    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=known, psk="rotatedpassword")
    await c._loop()
    assert c.state.phase == "done"
    assert c.state.found_pin == known
    assert c.state.found_psk == "rotatedpassword"


async def test_resume_pin_changed_resets_sweep(tmp_path):
    # AP admin changed the PIN to one with a different first half — verify
    # invalidates, full sweep restarts from "common".
    stored = pins.full_pin("1357", "246")
    new = pins.full_pin("9876", "543")          # different first half
    _write_done_state(tmp_path, stored, "oldpassword")
    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=new, psk="newpassword")
    # We don't run a full sweep here (it'd take 1k+ attempts); just trip the
    # first verify attempt by limiting the harness.
    orig_try = c._try
    attempts = []

    async def one_then_stop(pin):
        attempts.append(pin)
        out = await orig_try(pin)
        c.stopped = True                        # bail before resweeping
        return out
    c._try = one_then_stop
    await c._loop()
    assert attempts == [stored]                 # the verify pin
    # Verify saw FIRST_HALF_WRONG → full reset.
    assert c.state.found_pin is None
    assert c.state.found_psk is None
    assert c.state.first_half is None
    assert c.state.phase == "common"
    assert c.state.common_index == 0


async def test_second_half_sweep_skips_already_tested_dummy(tmp_path):
    # When first_half is confirmed via the first-half phase's dummy pin
    # (full_pin(p1, "000")), the second-half sweep must NOT re-emit that exact
    # pin — its middle ("000") is provably wrong (SECOND_HALF_WRONG) and just
    # wastes an attempt right after the phase transition.
    known = pins.full_pin("1357", "246")
    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=known, psk="pw")
    await c._loop()
    dummy = pins.full_pin("1357", "000")
    assert c.tried.count(dummy) == 1   # tried once (the discovery), never again


async def test_first_half_confirmed_switches_phase(tmp_path):
    known = pins.full_pin("2468", "135")
    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=known, psk="x")
    await c._loop()
    assert c.state.found_pin == known
    # Once "2468" matched, it must have pinned the first half and swept halves.
    assert c.state.first_half == "2468"


async def test_run_state_persisted_and_resumed(tmp_path):
    known = pins.full_pin("1357", "246")
    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=known, psk="pw")
    await c._loop()

    path = _state_path(str(tmp_path), "aa:bb:cc:dd:ee:ff")
    assert path.exists()
    import json
    data = json.loads(path.read_text())
    assert data["found_pin"] == known and data["phase"] == "done"

    # A fresh campaign loads the prior state.
    c2 = WpsCampaign(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    assert c2.state.found_pin == known


async def test_rate_limit_does_not_skip_untested_pin(tmp_path):
    # The AP refuses the first two sessions before the M4 oracle (rate-limiting):
    # PROTO_ERROR must NOT advance the keyspace, so the SAME pin is retried until
    # it's actually tested. (Regression for the skip-on-PROTO_ERROR bug.)
    known = pins.full_pin("1357", "246")

    class RateLimited(ScriptedCampaign):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.proto_left = 2          # < strike_threshold (3) so no real sleep

        async def _try(self, pin):
            if self.proto_left > 0:
                self.proto_left -= 1
                self.tried.append(pin)
                return AttemptOutcome(PinResult.PROTO_ERROR, pin, detail="rate-limit")
            return await super()._try(pin)

    c = RateLimited(_iface(), _target(), state_dir=str(tmp_path),
                    log=lambda m: None, known_pin=known, psk="pw")
    await c._loop()

    # First three sessions were all the SAME first pin (2 refused + 1 real test).
    assert c.tried[0] == c.tried[1] == c.tried[2] == pins.COMMON_PINS[0]
    assert c.state.found_pin == known
    # tested counts only real oracle results, never the rate-limited no-ops.
    assert c.state.tested < c.state.attempts


async def test_teardown_saves_state_and_clears_fake_mac(tmp_path):
    # The base lifecycle calls teardown() on every exit — it must checkpoint the
    # .run resume file and release the active-monitor MAC (the old _run finally).
    cleared = []

    async def _clear(*_a, **_k):
        cleared.append(True)

    iface = SimpleNamespace(access_points={}, set_fake_mac=_set_fake_mac, clear_fake_mac=_clear)
    c = WpsCampaign(iface, _target(), state_dir=str(tmp_path), log=lambda m: None)
    c.state.tested = 42
    await c.teardown()
    assert cleared == [True]
    assert _state_path(str(tmp_path), "aa:bb:cc:dd:ee:ff").exists()


def test_lock_backoff_grows_with_observation():
    from wifit3.engine.attacks.wps.lock import LockTracker
    lt = LockTracker(min_wait=30, max_wait=360, initial_wait=60)
    assert lt.backoff() == 60                 # no observations yet
    lt.begin_lock()
    lt._observed_durations.append(120.0)
    lt.end_lock()
    assert 130 <= lt.backoff() <= 140         # learned ~ max*1.1, clamped
