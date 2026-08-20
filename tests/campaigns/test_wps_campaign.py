"""Campaign sweep logic, driven by a scripted enrollee (no hardware).

The campaign's _try() is overridden to simulate an AP with a known PIN, so we
exercise the COMMON→first-half→second-half progression, the first-half-confirmed
switch, success/PSK capture, and .run resume, without a radio or fake enrollee.
"""

from types import SimpleNamespace

from wifit3.campaigns.wps import known_pins, pins
from wifit3.campaigns.pin import WpsCampaign, _state_path
from wifit3.campaigns.wps.registrar import AttemptOutcome, PinResult
from wifit3.dot11.wsc.crypto import pin_is_valid


def _target(bssid="02:00:00:00:00:ff", ssid="Net", ch=1):
    return SimpleNamespace(bssid=bssid, ssid=ssid, channel=ch, wps_locked=False)


async def _set_fake_mac(*_a, **_k):
    return None   # un-ACked path: campaign falls back to use_no_ack, as before active-monitor


async def _clear_fake_mac(*_a, **_k):
    return None


async def _noop_async(*_a, **_k):
    return None


def _driver():
    return SimpleNamespace()


def _iface():
    ns = SimpleNamespace(access_points={}, driver=_driver(), current_channel=1,
                         set_fake_mac=_set_fake_mac, clear_fake_mac=_clear_fake_mac,
                         enable_rx_acks=_noop_async, disable_rx_acks=_noop_async,
                         acks_seen=lambda _mac: 0)
    ns.select_iface = lambda channel: ns   # doubles as the WlanArray

    async def _set_channel(ch, *a, **k):
        ns.current_channel = ch
    ns.set_channel = _set_channel
    ns.register_own_mac = lambda mac: mac if isinstance(mac, str) else ":".join(f"{b:02x}" for b in mac)
    ns.unregister_own_mac = lambda _mac: None

    def _lease(channel=None, fake_mac=None, bssid=None, ack_tally=False, iface=None):
        from wifit3.wlan.lease import Lease
        return Lease(ns, iface or ns, channel=channel, fake_mac=fake_mac,
                     bssid=bssid, ack_tally=ack_tally)
    ns.lease = _lease
    return ns


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


async def test_campaign_finds_common_pin_fast(tmp_path, monkeypatch):
    monkeypatch.setattr(known_pins, "known_pins_for", lambda bssid: [])   # isolate COMMON phase
    known = "12345670"                       # in COMMON_PINS
    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=known, psk="pw")
    await c._loop()
    assert c.state.found_pin == known
    assert len(c.tried) == 1                 # found on the first common attempt


def _write_done_state(tmp_path, found_pin, found_psk, bssid="02:00:00:00:00:ff"):
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
    # PIN unchanged but the AP's password was rotated: verify picks up the
    # NEW PSK from the recovered exchange. The high-value scenario.
    known = pins.full_pin("1357", "246")
    _write_done_state(tmp_path, known, "oldpassword")
    c = ScriptedCampaign(_iface(), _target(), state_dir=str(tmp_path),
                         log=lambda m: None, known_pin=known, psk="rotatedpassword")
    await c._loop()
    assert c.state.phase == "done"
    assert c.state.found_pin == known
    assert c.state.found_psk == "rotatedpassword"


def test_resume_pin_changed_resets_sweep(tmp_path):
    # AP admin changed the PIN to one with a different first half: the resume-time verify
    # sees FIRST_HALF_WRONG and invalidates everything, restarting the sweep from "common".
    stored = pins.full_pin("1357", "246")
    _write_done_state(tmp_path, stored, "oldpassword")
    c = WpsCampaign(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    assert c.state.found_pin == stored
    c.state.phase = "verify"                     # _loop switches done→verify on resume
    c._apply_outcome(stored, AttemptOutcome(PinResult.FIRST_HALF_WRONG, stored))
    # Verify saw FIRST_HALF_WRONG → full reset.
    assert c.state.found_pin is None
    assert c.state.found_psk is None
    assert c.state.first_half is None
    assert c.state.phase == "common"
    assert c.state.phase == "common"
    assert c.state.common_index == 0


async def test_second_half_sweep_skips_already_tested_dummy(tmp_path):
    # When first_half is confirmed via the first-half phase's dummy pin
    # (full_pin(p1, "000")), the second-half sweep must NOT re-emit that exact
    # pin: its middle ("000") is provably wrong (SECOND_HALF_WRONG) and just
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

    path = _state_path(str(tmp_path), "02:00:00:00:00:ff")
    assert path.exists()
    import json
    data = json.loads(path.read_text())
    assert data["found_pin"] == known and data["phase"] == "done"

    # A fresh campaign loads the prior state.
    c2 = WpsCampaign(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    assert c2.state.found_pin == known


async def test_rate_limit_does_not_skip_untested_pin(tmp_path, monkeypatch):
    # The AP refuses the first two sessions before the M4 answer (rate-limiting):
    # PROTO_ERROR must NOT advance the keyspace, so the SAME pin is retried until
    # it's actually tested. (Regression for the skip-on-PROTO_ERROR bug.)
    monkeypatch.setattr(known_pins, "known_pins_for", lambda bssid: [])   # isolate COMMON phase
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
    # tested counts only real M4/M6 answers, never the rate-limited no-ops.
    assert c.state.tested < c.state.attempts


async def test_teardown_releases_lease_and_saves_state(tmp_path):
    # teardown() runs on every exit: it checkpoints the resume file and releases the
    # lease (which clears the armed active-monitor MAC). No run means no lease to release.
    cleared = []

    async def _clear(*_a, **_k):
        cleared.append(True)

    async def _armed(mac, bssid=None):
        return ":".join(f"{b:02x}" for b in mac)   # HW-ACK armed, so release clears it

    iface = _iface()
    iface.set_fake_mac = _armed
    iface.clear_fake_mac = _clear
    c = WpsCampaign(iface, _target(), state_dir=str(tmp_path), log=lambda m: None)
    c.state.tested = 42
    c._lease = iface.lease(channel=c.channel, fake_mac=c.our_mac, ack_tally=c._tx_ack)
    await c._lease.acquire()
    await c.teardown()
    assert cleared == [True]                       # lease release cleared the armed MAC
    assert c._lease is None
    assert _state_path(str(tmp_path), "02:00:00:00:00:ff").exists()


def test_lock_backoff_grows_with_observation():
    from wifit3.campaigns.wps.lock import LockTracker
    lt = LockTracker(min_wait=30, max_wait=360, initial_wait=60)
    assert lt.backoff() == 60                 # no observations yet
    lt.begin_lock()
    lt._observed_durations.append(120.0)
    lt.end_lock()
    assert 130 <= lt.backoff() <= 140         # learned ~ max*1.1, clamped


# ---- .run progress surfacing (load_run_state / run_progress_line) -----------

def test_load_run_state_roundtrip_and_missing(tmp_path):
    from wifit3.campaigns.pin import load_run_state
    assert load_run_state(str(tmp_path), "02:00:00:00:00:ff") is None   # nothing on disk
    _write_done_state(tmp_path, "12345670", "pw")
    st = load_run_state(str(tmp_path), "02:00:00:00:00:ff")
    assert st is not None and st.found_pin == "12345670" and st.phase == "done"
    # Corrupt file → None, not a crash.
    _state_path(str(tmp_path), "02:00:00:00:00:ff").write_text("{not json")
    assert load_run_state(str(tmp_path), "02:00:00:00:00:ff") is None


def test_run_progress_line_cracked_is_silent():
    from wifit3.campaigns.pin import CampaignState, run_progress_line
    # A cracked run is reported by the saved WPS PSK row, not by a progress line.
    assert run_progress_line(CampaignState(bssid="x", phase="done",
                                           found_pin="12345670")) is None


def test_run_progress_line_first_half_uses_11k():
    from wifit3.campaigns.pin import CampaignState, run_progress_line
    line = run_progress_line(CampaignState(bssid="x", phase="first_half", tested=3200))
    assert "3,200" in line and "11k" in line


def test_run_progress_line_second_half_uses_1k():
    from wifit3.campaigns.pin import CampaignState, run_progress_line
    line = run_progress_line(CampaignState(bssid="x", phase="second_half",
                                           first_half="1357", p2_index=970))
    assert "970" in line and "1k" in line and "1357" in line


def test_run_progress_line_exhausted():
    from wifit3.campaigns.pin import CampaignState, run_progress_line
    line = run_progress_line(CampaignState(bssid="x", phase="failed", tested=11000))
    assert "exhausted" in line


# ---- dead-first-half skip (no re-trying a ruled-out prefix) ------------------

def test_dead_first_half_skips_shared_prefix_common(tmp_path, monkeypatch):
    monkeypatch.setattr(known_pins, "known_pins_for", lambda bssid: [])   # isolate COMMON phase
    # 12345670 and 12345678 share first half "1234". Once 12345670 is first-half-wrong,
    # 12345678 is a guaranteed first-half-wrong too. It must be skipped.
    c = WpsCampaign(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    assert "12345670" in pins.COMMON_PINS and "12345678" in pins.COMMON_PINS

    seen = []
    for _ in range(len(pins.COMMON_PINS) + 5):
        p = c._next_pin()
        if p is None or c.state.phase != "common":
            break
        seen.append(p)
        c._apply_outcome(p, AttemptOutcome(PinResult.FIRST_HALF_WRONG, p))

    assert seen[0] == "12345670"
    assert "1234" in c.state.dead_first_halves
    assert "12345678" not in seen           # skipped: prefix already dead


def test_dead_first_half_skips_common_prefix_in_sweep(tmp_path):
    # A COMMON prefix ruled out (e.g. "0000" via 00000000) is not re-tried when the
    # first-half sweep reaches p1_index 0.
    c = WpsCampaign(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    c.state.phase = "first_half"
    c.state.dead_first_halves = ["0000"]
    c.state.p1_index = 0
    p = c._next_pin()
    assert p is not None and p[:4] != "0000"   # 0000 skipped
    assert c.state.p1_index == 1


# ---- BSSID-derived default PINs (known_pins) --------------------------------

def test_known_pins_for_generates_valid_candidates():
    from wifit3.campaigns.wps.known_pins import known_pins_for
    # Computed from the BSSID at runtime; separator- and case-insensitive.
    a = known_pins_for("00:11:22:33:44:55")
    b = known_pins_for("001122334455")
    assert a and a == b
    assert all(len(p) == 8 and p.isdigit() and pin_is_valid(p) for p in a)
    assert len(a) == len(set(a))                          # deduped
    assert known_pins_for("00:11:22") == []               # not a full MAC


def test_campaign_seeds_oui_pins_ahead_of_common(tmp_path):
    from wifit3.campaigns.wps.known_pins import known_pins_for
    oui_pins = known_pins_for("00:18:e7:aa:bb:cc")
    assert oui_pins                                       # generated from the BSSID
    c = WpsCampaign(_iface(), _target(bssid="00:18:e7:aa:bb:cc"),
                    state_dir=str(tmp_path), log=lambda m: None)
    assert c._oui_pin_count == len(oui_pins)
    assert c._common_pins[:len(oui_pins)] == oui_pins     # BSSID-derived pins first
    assert pins.COMMON_PINS[0] in c._common_pins          # then the generic list
    assert len(c._common_pins) == len(set(c._common_pins))  # deduped


def test_campaign_seeds_generated_pins_for_any_bssid(tmp_path):
    from wifit3.campaigns.wps.known_pins import known_pins_for
    # No OUI is "unknown" now: the generators fire for every BSSID.
    bssid = "fe:dc:ba:98:76:54"
    c = WpsCampaign(_iface(), _target(bssid=bssid),
                    state_dir=str(tmp_path), log=lambda m: None)
    assert c._oui_pin_count > 0
    assert c._common_pins[:c._oui_pin_count] == known_pins_for(bssid)
    assert all(p in c._common_pins for p in pins.COMMON_PINS)  # generic list still follows


# ---- lost-reply retry (timeout-as-NACK on a known-NACKing AP) ----------------

def test_lost_reply_retries_for_nacking_ap(tmp_path):
    c = WpsCampaign(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    # An explicit NACK (config_error set) proves the AP answers wrong guesses.
    c._should_retry_lost_reply("00000000", AttemptOutcome(
        PinResult.FIRST_HALF_WRONG, "00000000", config_error=18))
    assert c._ap_sends_nacks
    # A *silent* first-half-wrong is now a lost reply, not a rejection → retry.
    assert c._should_retry_lost_reply("01030006", AttemptOutcome(
        PinResult.FIRST_HALF_WRONG, "01030006", via_timeout=True)) is True


def test_lost_reply_no_retry_for_silent_ap(tmp_path):
    # No NACK ever seen → a silent timeout is genuine timeout-as-NACK; advance, don't retry.
    c = WpsCampaign(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    assert c._should_retry_lost_reply("01030006", AttemptOutcome(
        PinResult.FIRST_HALF_WRONG, "01030006", via_timeout=True)) is False


async def test_active_refusal_bails_not_churns(tmp_path):
    # An AP that actively refuses (disassoc / identity-stall) is given up on after _REFUSAL_BAIL
    # consecutive refusals, not soft-lock-churned forever. Mere silence would NOT bail.
    class Refusing(WpsCampaign):
        async def _try(self, pin):
            return AttemptOutcome(PinResult.TIMEOUT, pin, refused=True,
                                  detail="AP disassociated us (802.1X-auth-failed)")

    c = Refusing(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    await c._loop()
    assert c.status == "failed"
    assert c.state.tested == 0
    assert c.state.attempts == c._REFUSAL_BAIL       # bailed promptly, didn't churn the sweep


async def test_silence_does_not_bail(tmp_path):
    # A pure-silence TIMEOUT (refused=False) must NOT bail: infinite patience (could be a far AP).
    hits = {"n": 0}

    class Silent(WpsCampaign):
        async def _try(self, pin):
            hits["n"] += 1
            if hits["n"] >= 5:
                self.request_stop()                  # stop the otherwise-infinite retry
            return AttemptOutcome(PinResult.TIMEOUT, pin, detail="AP didn't respond")

    c = Silent(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    await c._loop()
    assert hits["n"] >= 5                             # kept retrying (no bail), until we stopped it


def test_config_error_setup_locked_locks_immediately(tmp_path):
    # An explicit WPS Setup-Locked NACK (config_error 15) is a lock, not a wrong PIN: lock at
    # once and do not advance the keyspace.
    c = WpsCampaign(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    before = c.state.common_index
    c._apply_outcome("12345670", AttemptOutcome(PinResult.PROTO_ERROR, "12345670", config_error=15))
    assert c.lock.is_locked(beacon_locked=False)
    assert c.state.common_index == before


def test_lost_reply_retry_is_bounded(tmp_path):
    c = WpsCampaign(_iface(), _target(), state_dir=str(tmp_path), log=lambda m: None)
    c._ap_sends_nacks = True
    out = AttemptOutcome(PinResult.FIRST_HALF_WRONG, "01030006", via_timeout=True)
    trues = 0
    for _ in range(50):
        if c._should_retry_lost_reply("01030006", out):
            trues += 1
        else:
            break
    assert trues == c._MAX_TIMEOUT_RETRIES   # concedes after the cap, doesn't wedge
