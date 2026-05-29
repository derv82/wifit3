"""Tests for WEP ARP replay: re-addressing, patient testing, P&O rate control.

The replay engine re-addresses each captured candidate into a ToDS frame from
our MAC before injecting, so the stub keys IV "gain" on the candidate's body
marker byte (which survives re-addressing) rather than the exact bytes.
"""
import asyncio

from wifit3.engine.models import AccessPoint
from wifit3.engine.attacks.wep.arp_replay import WepArpReplay

# Captured frames: FC = Data/FromDS/Protected (08 42), 24-byte header, then a
# body whose first byte marks the candidate.
GOOD = bytes([0x08, 0x42]) + b"\x00" * 22 + b"\xAA" * 44
BAD = bytes([0x08, 0x42]) + b"\x00" * 22 + b"\xBB" * 44


class FakeCollector:
    """Yields IVs based on the re-addressed frame's body marker (frame[24])."""

    def __init__(self, marker_yields: dict[int, int], candidates: list[bytes]):
        self._marker_yields = marker_yields
        self._candidates = candidates
        self._unique = 0

    def on_send(self, frame: bytes) -> None:
        if len(frame) > 24:
            self._unique += self._marker_yields.get(frame[24], 0)

    def unique_count(self, bssid: str) -> int:
        return self._unique

    def rate(self, bssid: str, now=None) -> float:
        return 0.0

    def arp_candidates(self, bssid: str) -> list[bytes]:
        return list(self._candidates)

    def arp_candidate_count(self, bssid: str) -> int:
        return len(self._candidates)

    def arp_seen_count(self, bssid: str) -> int:
        return len(self._candidates)


def _replay(mocker, collector, **kw):
    ap = AccessPoint(bssid="11:22:33:44:55:66", ssid="W", channel=6, encryption="WEP")
    iface = mocker.MagicMock()

    async def _send(frame, use_no_ack=True):
        collector.on_send(frame)
        return True

    iface.send_raw = _send
    r = WepArpReplay(iface, ap, collector, **kw)
    # Instant cycles for logic tests: a tiny burst (rate is now a per-window
    # packet COUNT) + a zero-length window (no 1s wait). start() re-reads
    # _PO_START_PPS, so override it too. P&O's own logic is tested directly.
    r._WINDOW_S = 0.0
    r._rate = 4
    r._PO_START_PPS = 4
    return r


def _capture_logs(mocker, collector, **kw):
    """Build a replay engine whose `_log` calls go into a list. Lets tests
    assert on what the event-log sees."""
    captured: list[str] = []
    r = _replay(mocker, collector, log_callback=captured.append, **kw)
    return r, captured


def test_begin_trial_log_includes_candidate_byte_count(mocker):
    """User-visible event log should show the trial candidate's byte
    length — helps spot FCS-padded or mis-classified candidates."""
    r, logs = _capture_logs(mocker, FakeCollector({}, []))
    cand = bytes([0x08, 0x42]) + b"\x00" * 22 + b"\xAA" * 44   # 68 B
    r._begin_trial(cand)
    assert any("(68 B)" in m for m in logs), logs


def test_replayable_leaf_includes_byte_count(mocker):
    """The 'replayable' branch on _judge() also names the byte count so
    user can confirm which length succeeded."""
    coll = FakeCollector({}, [])
    r, logs = _capture_logs(mocker, coll)
    cand = bytes([0x08, 0x42]) + b"\x00" * 22 + b"\xCC" * 44   # 68 B
    r._current = cand
    r._trial_gain = r._MIN_TRIAL_GAIN   # immediate winner
    r._judge(gain=r._MIN_TRIAL_GAIN)
    assert any("(68 B)" in m and "replayable" in m for m in logs), logs


def test_failed_to_replay_leaf_includes_byte_count(mocker):
    """The blacklist branch on _judge() also names the byte count."""
    import time as _time
    coll = FakeCollector({}, [])
    r, logs = _capture_logs(mocker, coll)
    cand = bytes([0x08, 0x42]) + b"\x00" * 22 + b"\xDD" * 60   # 84 B
    r._current = cand
    r._trial_gain = 0
    r._trial_started = _time.time() - r._TRIAL_WINDOW - 1     # past window
    r._judge(gain=0)
    assert any("(84 B)" in m and "failed to replay" in m for m in logs), logs


def test_build_replay_frame_readdresses_to_tods(mocker):
    r = _replay(mocker, FakeCollector({}, []), source_mac=b"\x02\x00\x00\x00\x00\x09")
    captured = bytes([0x08, 0x42]) + b"\x00" * 22 + b"\x03\xff\x00\x00" + b"\xde" * 40
    out = r._build_replay_frame(captured)
    assert out[0:2] == b"\x08\x41"                     # ToDS + Protected
    assert out[4:10] == r.bssid_bytes                  # Addr1 = BSSID
    assert out[10:16] == b"\x02\x00\x00\x00\x00\x09"   # Addr2 = our MAC
    assert out[16:22] == b"\xff\xff\xff\xff\xff\xff"    # Addr3 = broadcast
    assert out[24:] == captured[24:]                   # encrypted body preserved


async def test_locks_onto_yielding_candidate(mocker):
    coll = FakeCollector({0xAA: 5, 0xBB: 0}, [BAD, GOOD])
    r = _replay(mocker, coll)
    r._TRIAL_WINDOW = 0.0
    r.start()
    for _ in range(40):
        await asyncio.sleep(0)
        if r.stats.has_winner:
            break
    r.stop()
    assert r.stats.has_winner
    assert r._winner == GOOD
    assert BAD in r._failed


async def test_patient_does_not_blacklist_before_trial_window(mocker):
    coll = FakeCollector({0xAA: 0}, [GOOD])     # yields nothing *so far*
    r = _replay(mocker, coll)
    r._TRIAL_WINDOW = 999.0
    r.start()
    for _ in range(10):
        await asyncio.sleep(0)
    failed = set(r._failed)
    r.stop()
    assert GOOD not in failed


async def test_notify_activity_called_on_inject(mocker):
    coll = FakeCollector({0xAA: 5}, [GOOD])
    seen = {"n": 0}
    r = _replay(mocker, coll, notify_activity=lambda: seen.__setitem__("n", seen["n"] + 1))
    r.start()
    for _ in range(5):
        await asyncio.sleep(0)
    r.stop()
    assert seen["n"] > 0


async def test_unassociated_blocks_tx(mocker):
    """We only inject once ensure_associated() succeeds — a failing one (can't
    fake-auth) holds TX at 'waiting-auth' with nothing sent."""
    coll = FakeCollector({0xAA: 5}, [GOOD])

    async def never():
        return False
    r = _replay(mocker, coll, ensure_associated=never)
    r.start()
    for _ in range(5):
        await asyncio.sleep(0)
    state, injected = r.state, r.stats.injected
    r.stop()
    assert state == "waiting-auth"
    assert injected == 0


async def test_pause_resume(mocker):
    coll = FakeCollector({0xAA: 5}, [GOOD])
    r = _replay(mocker, coll)
    r.start()
    r.pause()
    for _ in range(5):
        await asyncio.sleep(0)
    assert r.state == "paused"
    r.resume()
    r.stop()


# ---- P&O rate controller (per-1s-window) -----------------------------------
# Drive _maybe_adjust_rate directly: set the last window's IVs/s + prev, call it.


def _po_setup(mocker, rate=100.0, step=32.0, prev=50.0):
    r = _replay(mocker, FakeCollector({0xAA: 5}, [GOOD]))
    r.state = "replaying"
    r._rate = rate
    r._rate_step = step
    r._po_prev_ivs_s = prev
    return r


def test_po_keeps_direction_when_ivs_improve(mocker):
    r = _po_setup(mocker, rate=100.0, step=32.0, prev=50.0)
    r._ivs_ewma = 60.0             # smoothed 60 > prev 50 → improved
    r._maybe_adjust_rate()
    assert r._rate_step == 32.0     # same direction
    assert r._rate == 132.0


def test_po_reverses_when_ivs_drop(mocker):
    r = _po_setup(mocker, rate=100.0, step=32.0, prev=50.0)
    r._ivs_ewma = 30.0            # smoothed 30 < 50 (beyond deadband) → worse
    r._maybe_adjust_rate()
    assert r._rate_step == -32.0    # reversed
    assert r._rate == 68.0


def test_po_holds_and_resets_baseline_when_not_replaying(mocker):
    r = _po_setup(mocker)
    r.state = "testing"
    r._maybe_adjust_rate()
    assert r._rate == 100.0          # unchanged while not replaying
    assert r._po_prev_ivs_s == -1.0  # baseline reset for the next replay run
    assert r._ivs_ewma == -1.0       # smoothing reset too


def test_po_clamps_to_max(mocker):
    r = _po_setup(mocker, rate=985.0, step=32.0, prev=50.0)
    r._ivs_ewma = 80.0            # improving → keeps +step → 1017, clamped
    r._maybe_adjust_rate()
    assert r._rate == r._PO_MAX_PPS
