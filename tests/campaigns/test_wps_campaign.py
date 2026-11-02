"""Campaign sweep logic, driven by a scripted enrollee (no hardware).

The campaign's _try() is overridden to simulate an AP with a known PIN, so we
exercise the COMMON→first-half→second-half progression, the first-half-confirmed
switch, success/PSK capture, and .run resume, without a radio or fake enrollee.
"""

import asyncio
import concurrent.futures
import multiprocessing.connection
import threading
import time
from concurrent.futures.process import BrokenProcessPool
from types import SimpleNamespace

import pytest

from wifit3.campaigns import pin as pin_mod
from wifit3.campaigns.wps import known_pins, pins
from wifit3.campaigns.pin import WpsCampaign, _state_path
from wifit3.campaigns.wps.pixie import PixieBundle, PixieMode, PixieResult
from wifit3.campaigns.wps.registrar import AttemptOutcome, PinResult
from wifit3.dot11.wsc import crypto as wc
from wifit3.models import AccessPoint
from wifit3.dot11.wsc.crypto import pin_is_valid


def _target(bssid="02:00:00:00:00:ff", ssid="Net", ch=1):
    return AccessPoint(bssid=bssid, ssid=ssid, channel=ch)


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


PIXIE_PIN = pins.full_pin("1357", "246")


def _pixie_bundle(pin: str, e_s1: bytes, e_s2: bytes) -> PixieBundle:
    authkey = bytes.fromhex("11" * wc.AUTHKEY_LEN)
    pke = bytes.fromhex("22" * wc.PUBKEY_LEN)
    pkr = bytes.fromhex("33" * wc.PUBKEY_LEN)
    psk1, psk2 = wc.derive_psk(authkey, pin)
    return PixieBundle(
        pke=pke,
        pkr=pkr,
        e_hash1=wc.e_or_r_hash(authkey, e_s1, psk1, pke, pkr),
        e_hash2=wc.e_or_r_hash(authkey, e_s2, psk2, pke, pkr),
        # Top bit set in the first word. A real glibc nonce never has it, so a no-match bundle
        # skips the RTL819x +/-1-day seed sweep instead of paying 0.7s on every test run.
        e_nonce=bytes.fromhex("c4" + "44" * (wc.NONCE_LEN - 1)),
        authkey=authkey,
    )


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

    c = ScriptedCampaign(_iface(), _target(),
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
    c = ScriptedCampaign(_iface(), _target(),
                         log=lambda m: None, known_pin=known, psk="pw")
    await c._loop()
    assert c.state.found_pin == known
    assert len(c.tried) == 1                 # found on the first common attempt


async def test_campaign_tries_pixie_before_online_sweep(tmp_path, monkeypatch):
    monkeypatch.setattr(known_pins, "known_pins_for", lambda bssid: [])
    known = pins.full_pin("1357", "246")
    zero = b"\x00" * wc.SECRET_NONCE_LEN

    class PixieVulnerable(ScriptedCampaign):
        async def _try(self, pin):
            self.tried.append(pin)
            if pin == known:
                return AttemptOutcome(PinResult.SUCCESS, pin, psk=self.psk, ssid="Net")
            return AttemptOutcome(PinResult.FIRST_HALF_WRONG, pin,
                                  pixie=_pixie_bundle(known, zero, zero))

    c = PixieVulnerable(_iface(), _target(),
                        log=lambda m: None, known_pin=known, psk="pw")
    await c._loop()

    assert c.status == "found"
    assert c.state.found_pin == known
    assert c.state.found_psk == "pw"
    assert c.tried == [pins.COMMON_PINS[0], known]


async def test_campaign_falls_back_when_pixie_has_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(known_pins, "known_pins_for", lambda bssid: [])
    known = pins.full_pin("0001", "000")
    e_s1 = bytes.fromhex("12" * wc.SECRET_NONCE_LEN)
    e_s2 = bytes.fromhex("34" * wc.SECRET_NONCE_LEN)

    class PixieNotVulnerable(ScriptedCampaign):
        async def _try(self, pin):
            out = await super()._try(pin)
            if len(self.tried) == 1:
                out.pixie = _pixie_bundle(known, e_s1, e_s2)
            return out

    c = PixieNotVulnerable(_iface(), _target(),
                           log=lambda m: None, known_pin=known, psk="pw")
    await c._loop()

    assert c.status == "found"
    assert c.state.found_pin == known
    assert c.tried[:2] == [pins.COMMON_PINS[0], pins.COMMON_PINS[1]]
    assert len(c.tried) > 2


async def test_campaign_resumes_the_sweep_when_a_pixie_half_is_wrong(tmp_path, monkeypatch):
    # PixieDust can prove a first half the AP never confirmed (RTL819x, E-S2's seed missed). When
    # that half is wrong the second-half phase has to retire it and resume the sweep.
    monkeypatch.setattr(known_pins, "known_pins_for", lambda bssid: [])
    known = pins.full_pin("0001", "000")
    zero = b"\x00" * wc.SECRET_NONCE_LEN

    class PixieWrongHalf(ScriptedCampaign):
        async def _try(self, pin):
            if len(self.tried) >= 40:
                self.stopped = True   # a sweep stuck on one PIN would never return on its own
            out = await super()._try(pin)
            if len(self.tried) == 1:
                out.pixie = _pixie_bundle(known, zero, zero)
            return out

        async def _run_pixie(self, bundle):
            return PixieResult(first_half="9999", mode=PixieMode.RTL819X)

    c = PixieWrongHalf(_iface(), _target(), log=lambda m: None, known_pin=known, psk="pw")
    await c._loop()

    wrong = pins.full_pin("9999", "000")
    assert c.status == "found"
    assert c.state.found_pin == known
    assert "9999" in c.state.dead_first_halves
    assert c.tried[1] == wrong and c.tried.count(wrong) == 1


async def test_campaign_verifies_and_reports_an_empty_pin(tmp_path, monkeypatch):
    # A zero-length device password is a real PixieDust outcome. "" is falsy, so the campaign has
    # to carry it as a recovered PIN and render it as <empty> rather than drop the win.
    monkeypatch.setattr(known_pins, "known_pins_for", lambda bssid: [])
    zero = b"\x00" * wc.SECRET_NONCE_LEN

    class PixieEmptyPin(ScriptedCampaign):
        async def _try(self, pin):
            out = await super()._try(pin)
            if len(self.tried) == 1:
                out.pixie = _pixie_bundle("", zero, zero)
            return out

        async def _run_pixie(self, bundle):
            return PixieResult(pin="", mode=PixieMode.NULL_SECRET, found=True)

    c = PixieEmptyPin(_iface(), _target(), log=lambda m: None, known_pin="", psk="pw")
    await c._loop()

    assert c.status == "found"
    assert c.state.found_pin == ""
    assert c.state.found_psk == "pw"
    assert c.tried == [pins.COMMON_PINS[0], ""]
    assert "<empty>" in pin_mod.wps_status_markup(c)
    assert pin_mod.run_progress_line(c.state) is None


# ----- PixieDust offload: the sweep must never make Stop or process exit wait on it -----

class _FakeWorker:
    def __init__(self):
        self.killed = False

    def kill(self):
        self.killed = True


class _FakePool:
    """Stands in for ProcessPoolExecutor to assert how the pool is dropped, with no worker to
    spawn. ``shutdown`` clears the process map exactly as the real executor does."""

    def __init__(self):
        self._processes = {1: _FakeWorker()}
        self.shutdown_kwargs = None

    def shutdown(self, **kwargs):
        self.shutdown_kwargs = kwargs
        self._processes = None


def _pixie_campaign(known=PIXIE_PIN):
    c = ScriptedCampaign(_iface(), _target(), log=lambda m: None, known_pin=known, psk="pw")
    c._PIXIE_POLL = 0.001   # the production 0.2s Stop cadence only slows the tests down
    return c


def _no_process_pool(*_a, **_k):
    raise OSError("no subprocesses in this environment")


def _parked_recover(started, release):
    """A search that parks until released, so Stop/cancel can be caught mid-flight."""
    def recover(_bundle):
        started.set()
        release.wait(5)
        return PixieResult(pin="99999999", found=True)
    return recover


async def _await_started(started):
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)


async def test_pixie_search_runs_in_a_worker_process(monkeypatch):
    # The only test here that spawns a worker. The bundle is null-secret, so the search hits on
    # its first mode and this costs a spawn and nothing more.
    created, workers = [], []

    class RecordingPool(concurrent.futures.ProcessPoolExecutor):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            assert isinstance(self._processes, dict)   # _halt_pixie relies on this private map
            created.append(self)

        def submit(self, *a, **kw):
            future = super().submit(*a, **kw)
            workers.extend(self._processes.values())   # populated by submit, cleared by shutdown
            return future

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", RecordingPool)
    c = _pixie_campaign()
    zero = bytes(wc.SECRET_NONCE_LEN)

    result = await c._run_pixie(_pixie_bundle(PIXIE_PIN, zero, zero))

    assert result.found and result.pin == PIXIE_PIN
    assert len(created) == 1       # the search really left the event loop's process
    assert c._pixie_pool is None   # and the worker was dropped once it answered
    assert workers, "the search never reached a worker process"
    for worker in workers:
        # Wait on the sentinel, not is_alive(): the pool's manager thread reaps these workers too,
        # and the thread that loses the waitpid race sees ECHILD, which is_alive() reports as alive.
        died = multiprocessing.connection.wait([worker.sentinel], timeout=10)
        assert died, "the search worker outlived the campaign"


async def test_pixie_falls_back_to_a_thread_without_subprocesses(monkeypatch):
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _no_process_pool)
    c = _pixie_campaign()
    zero = bytes(wc.SECRET_NONCE_LEN)

    result = await c._run_pixie(_pixie_bundle(PIXIE_PIN, zero, zero))

    assert result.found and result.pin == PIXIE_PIN


class _BrokenOnUsePool:
    """Constructs fine, then fails on first use. ProcessPoolExecutor spawns its workers lazily,
    so an unusable subprocess environment surfaces out of the awaited future, not the ctor."""

    def __init__(self, *_a, **_k):
        self._processes = {}
        self.shutdown_kwargs = None

    def submit(self, *_a, **_k):
        broken = concurrent.futures.Future()
        broken.set_exception(BrokenProcessPool("boom"))
        return broken

    def shutdown(self, **kwargs):
        self.shutdown_kwargs = kwargs
        self._processes = None


async def test_pixie_falls_back_to_a_thread_when_the_pool_breaks_on_use(monkeypatch):
    pools = []

    def _broken_on_use(*a, **kw):
        pools.append(_BrokenOnUsePool(*a, **kw))
        return pools[-1]

    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _broken_on_use)
    c = _pixie_campaign()
    zero = bytes(wc.SECRET_NONCE_LEN)

    result = await c._run_pixie(_pixie_bundle(PIXIE_PIN, zero, zero))

    assert result.found and result.pin == PIXIE_PIN   # the thread fallback still answered
    assert len(pools) == 1                            # the process pool was tried first
    assert pools[0].shutdown_kwargs == {"wait": False, "cancel_futures": True}
    assert c._pixie_pool is None


async def test_stop_abandons_the_pixie_search(monkeypatch):
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _no_process_pool)
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(pin_mod, "recover_pin", _parked_recover(started, release))
    c = _pixie_campaign()
    try:
        search = asyncio.ensure_future(c._run_pixie(object()))
        await _await_started(started)
        c.stopped = True

        result = await asyncio.wait_for(search, timeout=5)

        assert result.found is False   # abandoned, not the answer the sweep would have given
        assert c._pixie_pool is None
    finally:
        release.set()


async def test_cancelling_the_search_does_not_join_it(monkeypatch):
    # Quitting the app cancels the campaign task. Leaving the search must not wait on it.
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _no_process_pool)
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(pin_mod, "recover_pin", _parked_recover(started, release))
    c = _pixie_campaign()
    try:
        search = asyncio.ensure_future(c._run_pixie(object()))
        await _await_started(started)

        search.cancel()
        t0 = time.perf_counter()
        with pytest.raises(asyncio.CancelledError):
            await search
        elapsed = time.perf_counter() - t0

        assert elapsed < 2.0, f"cancellation joined the search ({elapsed:.1f}s of a 5s sweep)"
        assert c._pixie_pool is None
    finally:
        release.set()


async def test_stopped_search_does_not_claim_there_were_no_matches(monkeypatch):
    # A search cut short by Stop never looked; saying "no matches" would be a lie.
    monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor", _no_process_pool)
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(pin_mod, "recover_pin", _parked_recover(started, release))
    zero = bytes(wc.SECRET_NONCE_LEN)
    logged = []
    c = ScriptedCampaign(_iface(), _target(), log=logged.append, known_pin=PIXIE_PIN, psk="pw")
    c._PIXIE_POLL = 0.001
    out = AttemptOutcome(PinResult.FIRST_HALF_WRONG, PIXIE_PIN,
                         pixie=_pixie_bundle(PIXIE_PIN, zero, zero))
    try:
        attempt = asyncio.ensure_future(c._try_pixie(PIXIE_PIN, out))
        await _await_started(started)
        c.stopped = True

        assert await asyncio.wait_for(attempt, timeout=5) is False
        assert not any("no PixieDust matches" in m for m in logged)
    finally:
        release.set()


async def test_halt_pixie_kills_the_worker_instead_of_waiting():
    c = _pixie_campaign()
    pool = _FakePool()
    worker = pool._processes[1]
    c._pixie_pool = pool

    c._halt_pixie()

    assert pool.shutdown_kwargs == {"wait": False, "cancel_futures": True}
    assert worker.killed, "workers must be captured before shutdown() clears the map"
    assert c._pixie_pool is None


async def test_teardown_halts_the_pixie_pool():
    c = _pixie_campaign()
    pool = _FakePool()
    c._pixie_pool = pool

    c._teardown()

    assert pool.shutdown_kwargs is not None
    assert c._pixie_pool is None
    c._teardown()   # idempotent: no pool left to halt


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
    c = ScriptedCampaign(_iface(), _target(),
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
    c = ScriptedCampaign(_iface(), _target(),
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
    c = WpsCampaign(_iface(), _target(), log=lambda m: None)
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
    c = ScriptedCampaign(_iface(), _target(),
                         log=lambda m: None, known_pin=known, psk="pw")
    await c._loop()
    dummy = pins.full_pin("1357", "000")
    assert c.tried.count(dummy) == 1   # tried once (the discovery), never again


async def test_first_half_confirmed_switches_phase(tmp_path):
    known = pins.full_pin("2468", "135")
    c = ScriptedCampaign(_iface(), _target(),
                         log=lambda m: None, known_pin=known, psk="x")
    await c._loop()
    assert c.state.found_pin == known
    # Once "2468" matched, it must have pinned the first half and swept halves.
    assert c.state.first_half == "2468"


async def test_run_state_persisted_and_resumed(tmp_path):
    known = pins.full_pin("1357", "246")
    c = ScriptedCampaign(_iface(), _target(),
                         log=lambda m: None, known_pin=known, psk="pw")
    await c._loop()

    path = _state_path(str(tmp_path), "02:00:00:00:00:ff")
    assert path.exists()
    import json
    data = json.loads(path.read_text())
    assert data["found_pin"] == known and data["phase"] == "done"

    # A fresh campaign loads the prior state.
    c2 = WpsCampaign(_iface(), _target(), log=lambda m: None)
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

    c = RateLimited(_iface(), _target(),
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
    c = WpsCampaign(iface, _target(), log=lambda m: None)
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
    c = WpsCampaign(_iface(), _target(), log=lambda m: None)
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


def test_a_changed_pin_forgets_the_old_pins_dead_first_halves(monkeypatch):
    monkeypatch.setattr(known_pins, "known_pins_for", lambda bssid: ["00025614", "20001322"])
    c = WpsCampaign(_iface(), _target(), log=lambda m: None)
    c.state.dead_first_halves = ["0002", "2000"]
    c.state.found_pin = "12345670"
    c.state.phase = "verify"

    c._apply_verify_outcome("12345670", AttemptOutcome(PinResult.FIRST_HALF_WRONG, "12345670"))

    assert c.state.dead_first_halves == ["1234"]
    assert c._next_pin() == "00025614"


def test_dead_first_half_skips_common_prefix_in_sweep(tmp_path):
    # A COMMON prefix ruled out (e.g. "0000" via 00000000) is not re-tried when the
    # first-half sweep reaches p1_index 0.
    c = WpsCampaign(_iface(), _target(), log=lambda m: None)
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
    assert all(len(p) == 8 and p.isdigit() for p in a)
    assert len(a) == len(set(a))                          # deduped
    assert known_pins_for("00:11:22") == []               # not a full MAC


def test_campaign_seeds_oui_pins_ahead_of_common(tmp_path):
    from wifit3.campaigns.wps.known_pins import known_pins_for
    oui_pins = known_pins_for("00:18:e7:aa:bb:cc")
    assert oui_pins                                       # generated from the BSSID
    c = WpsCampaign(_iface(), _target(bssid="00:18:e7:aa:bb:cc"),
                    log=lambda m: None)
    assert c._oui_pin_count == len(oui_pins)
    assert c._common_pins[:len(oui_pins)] == oui_pins     # BSSID-derived pins first
    assert pins.COMMON_PINS[0] in c._common_pins          # then the generic list
    assert len(c._common_pins) == len(set(c._common_pins))  # deduped


def test_campaign_seeds_generated_pins_for_any_bssid(tmp_path):
    from wifit3.campaigns.wps.known_pins import known_pins_for
    # No OUI is "unknown" now: the generators fire for every BSSID.
    bssid = "fe:dc:ba:98:76:54"
    c = WpsCampaign(_iface(), _target(bssid=bssid),
                    log=lambda m: None)
    assert c._oui_pin_count > 0
    assert c._common_pins[:c._oui_pin_count] == known_pins_for(bssid)
    assert all(p in c._common_pins for p in pins.COMMON_PINS)  # generic list still follows


# ---- lost-reply retry (timeout-as-NACK on a known-NACKing AP) ----------------

def test_lost_reply_retries_for_nacking_ap(tmp_path):
    c = WpsCampaign(_iface(), _target(), log=lambda m: None)
    # An explicit NACK (config_error set) proves the AP answers wrong guesses.
    c._should_retry_lost_reply("00000000", AttemptOutcome(
        PinResult.FIRST_HALF_WRONG, "00000000", config_error=18))
    assert c._ap_sends_nacks
    # A *silent* first-half-wrong is now a lost reply, not a rejection → retry.
    assert c._should_retry_lost_reply("01030006", AttemptOutcome(
        PinResult.FIRST_HALF_WRONG, "01030006", via_timeout=True)) is True


def test_lost_reply_no_retry_for_silent_ap(tmp_path):
    # No NACK ever seen → a silent timeout is genuine timeout-as-NACK; advance, don't retry.
    c = WpsCampaign(_iface(), _target(), log=lambda m: None)
    assert c._should_retry_lost_reply("01030006", AttemptOutcome(
        PinResult.FIRST_HALF_WRONG, "01030006", via_timeout=True)) is False


async def test_active_refusal_bails_not_churns(tmp_path):
    # An AP that actively refuses (disassoc / identity-stall) is given up on after _REFUSAL_BAIL
    # consecutive refusals, not soft-lock-churned forever. Mere silence would NOT bail.
    class Refusing(WpsCampaign):
        async def _try(self, pin):
            return AttemptOutcome(PinResult.TIMEOUT, pin, refused=True,
                                  detail="AP disassociated us (802.1X-auth-failed)")

    c = Refusing(_iface(), _target(), log=lambda m: None)
    await c._loop()
    assert c.status == "failed"
    assert c.state.tested == 0
    assert c.state.attempts == c._REFUSAL_BAIL       # bailed promptly, didn't churn the sweep


async def test_permanent_wps_lock_bails_after_zero_progress_cycles(tmp_path):
    logs = []

    class PermanentLock(WpsCampaign):
        async def _handle_lock(self, beacon_locked: bool, wait: bool = True) -> None:
            self.lock.end_lock()

    target = _target()
    target.wps_locked = True
    iface = _iface()
    iface.access_points[target.bssid.lower()] = target
    c = PermanentLock(iface, target, log=logs.append)
    await c._loop()

    assert c.status == "failed"
    assert c.state.tested == 0
    assert c._consecutive_locks_no_progress == c._MAX_LOCKS_NO_PROGRESS
    assert c.fail_reason == "WPS stayed locked for 5 cycles without PIN progress"


def test_lock_counter_resets_after_pin_progress(tmp_path):
    c = WpsCampaign(_iface(), _target(), log=lambda m: None)
    c._consecutive_locks_no_progress = 4

    c._apply_outcome("12345670", AttemptOutcome(PinResult.FIRST_HALF_WRONG, "12345670"))

    assert c._consecutive_locks_no_progress == 0


async def test_silence_does_not_bail(tmp_path):
    # A pure-silence TIMEOUT (refused=False) must NOT bail: infinite patience (could be a far AP).
    hits = {"n": 0}

    class Silent(WpsCampaign):
        async def _try(self, pin):
            hits["n"] += 1
            if hits["n"] >= 5:
                self.request_stop()                  # stop the otherwise-infinite retry
            return AttemptOutcome(PinResult.TIMEOUT, pin, detail="AP didn't respond")

    c = Silent(_iface(), _target(), log=lambda m: None)
    await c._loop()
    assert hits["n"] >= 5                             # kept retrying (no bail), until we stopped it


def test_config_error_setup_locked_locks_immediately(tmp_path):
    # An explicit WPS Setup-Locked NACK (config_error 15) is a lock, not a wrong PIN: lock at
    # once and do not advance the keyspace.
    c = WpsCampaign(_iface(), _target(), log=lambda m: None)
    before = c.state.common_index
    c._apply_outcome("12345670", AttemptOutcome(PinResult.PROTO_ERROR, "12345670", config_error=15))
    assert c.lock.is_locked(beacon_locked=False)
    assert c.state.common_index == before


def test_lost_reply_retry_is_bounded(tmp_path):
    c = WpsCampaign(_iface(), _target(), log=lambda m: None)
    c._ap_sends_nacks = True
    out = AttemptOutcome(PinResult.FIRST_HALF_WRONG, "01030006", via_timeout=True)
    trues = 0
    for _ in range(50):
        if c._should_retry_lost_reply("01030006", out):
            trues += 1
        else:
            break
    assert trues == c._MAX_TIMEOUT_RETRIES   # concedes after the cap, doesn't wedge


def test_wps_status_shows_fail_reason():
    import types
    from wifit3.campaigns.pin import wps_status_markup
    camp = types.SimpleNamespace(
        state=types.SimpleNamespace(found_pin=None, tested=0, phase="common", first_half=None),
        status="failed",
        fail_reason="WPS stayed locked for 5 cycles without PIN progress",
        eta_seconds=None,
        lock_kind=None,
    )

    line = wps_status_markup(camp)

    assert "failed" in line
    assert "stayed locked" in line
