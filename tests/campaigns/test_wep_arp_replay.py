"""Tests for WEP ARP replay: re-addressing, patient testing, P&O rate control.

The replay engine re-addresses each captured candidate into a ToDS frame from
our MAC before injecting. The verdict ("replayable") keys on the AP echoing OUR
frame back, so the stub, keyed on the candidate's body marker byte (which
survives re-addressing), drives BOTH the collector's IV gain AND whether a
synthetic AP echo is delivered to the engine's _rx_cb.
"""
import asyncio
from types import SimpleNamespace

from wifit3.models import AccessPoint
from wifit3.campaigns.wep.arp_replay import WepArpReplay

# Captured frames: FC = Data/FromDS/Protected (08 42), 24-byte header, then a
# body whose first byte marks the candidate.
GOOD = bytes([0x08, 0x42]) + b"\x00" * 22 + b"\xAA" * 44
BAD = bytes([0x08, 0x42]) + b"\x00" * 22 + b"\xBB" * 44


def _echo_frame(r) -> bytes:
    """An AP rebroadcast of our replay: Data/FromDS/Protected, DA=broadcast,
    Addr3(SA)=our MAC, fresh IV: the signature WepArpReplay._rx_cb matches."""
    return (
        bytes([0x08, 0x42])          # Data, FromDS, Protected
        + b"\x00\x00"                # Duration
        + b"\xff" * 6                # Addr1 (DA) = broadcast
        + r.bssid_bytes              # Addr2 = BSSID/TA
        + r.source_mac               # Addr3 (SA) = us
        + b"\x00\x00"                # Seq
        + b"\x11\x22\x33\x04"        # IV(3) + KeyID(1)
    )


class FakeCollector:
    """Yields IVs based on the re-addressed frame's body marker (frame[24]).

    ``echo`` controls whether a good-marker candidate also elicits a synthetic
    AP echo (the _rx_cb signal). echo=False models IV churn from ANOTHER station
    (e.g. a phone): the global count rises but the AP never echoes OURS.
    """

    def __init__(
        self, marker_yields: dict[int, int], candidates: list[bytes],
        echo: bool = True,
    ):
        self._marker_yields = marker_yields
        self._candidates = candidates
        self._echo = echo
        self._unique = 0

    def on_send(self, frame: bytes) -> None:
        if len(frame) > 24:
            self._unique += self._marker_yields.get(frame[24], 0)

    def echoes(self, frame: bytes) -> bool:
        """Whether the AP would echo this replay (keyed on the same body marker
        as IV yield): the echo check the test _rx_cb uses."""
        return (
            self._echo and len(frame) > 24
            and self._marker_yields.get(frame[24], 0) > 0
        )

    def unique_count(self, bssid: str) -> int:
        return self._unique

    def rate(self, bssid: str, now=None) -> float:
        return 0.0

    def arp_candidates(self, bssid: str) -> list[bytes]:
        return list(self._candidates)

    def arp_candidate_count(self, bssid: str) -> int:
        return len(self._candidates)

    def broadcast_seen_count(self, bssid: str) -> int:
        return len(self._candidates)


def _replay(mocker, collector, **kw):
    ap = AccessPoint(bssid="11:22:33:44:55:66", ssid="W", channel=6, encryption="WEP")
    iface = mocker.MagicMock()

    async def _send(frame, use_no_ack=True):
        collector.on_send(frame)
        # Simulate the AP echoing our replay back when this candidate is
        # genuinely replayable, exactly the frame _rx_cb correlates.
        if collector.echoes(frame):
            r._rx_cb(SimpleNamespace(raw=_echo_frame(r)))
        return True

    iface.send_raw = _send
    iface.send_no_wait = _send
    r = WepArpReplay(iface, ap, collector, **kw)
    # Instant cycles for logic tests: a tiny per-window inject count + a zero-length window (no
    # wait). start() re-seeds the target from _CTRL_START, so override that too. The controller's
    # own logic is tested directly below.
    r._WINDOW_S = 0.0
    r._target = 4
    r._CTRL_START = 4
    return r


def _capture_logs(mocker, collector, **kw):
    """Build a replay engine whose `_log` calls go into a list. Lets tests
    assert on what the event-log sees."""
    captured: list[str] = []
    r = _replay(mocker, collector, log_callback=captured.append, **kw)
    return r, captured


def test_begin_trial_log_includes_candidate_byte_count(mocker):
    """User-visible event log should show the trial candidate's byte
    length, helps spot FCS-padded or mis-classified candidates."""
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


async def test_iv_churn_without_echo_is_not_replayable(mocker):
    """The regression: another client's IVs (a phone) bump the global count,
    but the AP never echoes OUR frame. The candidate must NOT be promoted: it
    gets blacklisted once the trial window elapses."""
    coll = FakeCollector({0xAA: 5}, [GOOD], echo=False)   # IVs rise, no echo
    r = _replay(mocker, coll)
    r._TRIAL_WINDOW = 0.0
    r.start()
    for _ in range(40):
        await asyncio.sleep(0)
        if GOOD in r._failed:
            break
    has_winner, failed = r.stats.has_winner, set(r._failed)
    r.stop()
    assert not has_winner
    assert GOOD in failed


async def test_paused_echo_does_not_count(mocker):
    """A ChopChop relay shares our source_mac and matches the echo
    signature; arriving while replay is paused it must NOT count (the pause
    gate), but the SAME frame counts once resumed, proving it's the gate, not
    a signature mismatch, that dropped it."""
    coll = FakeCollector({0xAA: 5}, [GOOD])
    r = _replay(mocker, coll)
    r.start()
    r.pause()
    before = r._echoes
    r._rx_cb(SimpleNamespace(raw=_echo_frame(r)))
    assert r._echoes == before          # dropped while paused
    r.resume()
    r._rx_cb(SimpleNamespace(raw=_echo_frame(r)))
    assert r._echoes == before + 1      # counted once unpaused
    r.stop()


async def test_unassociated_blocks_tx(mocker):
    """We only inject once ensure_associated() succeeds: a failing one (can't
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


# ---- climb-with-peak-memory rate controller --------------------------------
# Drive _maybe_adjust_rate directly: set state=replaying + this window's echo rate, call it.
# Setting _echo_ewma == _last_echo_rate makes the internal EWMA an identity, so asserts are exact.


def _ctrl_setup(mocker, target=100.0, best_echo=50.0, best_target=100.0, echo=100.0):
    r = _replay(mocker, FakeCollector({0xAA: 5}, [GOOD]))
    r.state = "replaying"
    r._ctrl_climbing = True
    r._ctrl_misses = 0
    r._target = target
    r._best_echo = best_echo
    r._best_target = best_target
    r._echo_ewma = echo          # == last, so the EWMA update is an identity
    r._last_echo_rate = echo
    return r


def test_ctrl_climbs_when_echo_improves(mocker):
    r = _ctrl_setup(mocker, target=100.0, best_echo=50.0, echo=100.0)
    r._maybe_adjust_rate()
    assert r._best_echo == 100.0 and r._best_target == 100.0   # new peak remembered
    assert r._target == 125.0                                  # 100 * _CTRL_GROW, keep climbing
    assert r._ctrl_climbing


def test_ctrl_settles_back_after_two_bad_windows(mocker):
    # Climbed past the peak; echoes dropped. One bad window is tolerated (relay lag); the second
    # snaps back to best_target and stops climbing.
    r = _ctrl_setup(mocker, target=156.0, best_echo=100.0, best_target=100.0, echo=60.0)
    r._maybe_adjust_rate()
    assert r._ctrl_climbing and r._ctrl_misses == 1            # one lagged window tolerated
    r._echo_ewma = r._last_echo_rate = 60.0
    r._maybe_adjust_rate()
    assert not r._ctrl_climbing
    assert r._target == 100.0                                  # settled back to the remembered peak


def test_ctrl_holds_then_reprobes(mocker):
    r = _ctrl_setup(mocker, target=100.0, best_echo=100.0, best_target=100.0, echo=100.0)
    r._ctrl_climbing = False
    r._ctrl_since_probe = r._CTRL_REPROBE_WINDOWS - 1
    r._maybe_adjust_rate()
    assert r._ctrl_climbing and r._ctrl_since_probe == 0       # re-probe kicked in


def test_ctrl_below_ceiling_climbs_toward_all_gas(mocker):
    # Echoes keep rising with the target (below the AP's relay ceiling): the target grows every
    # window -> drives toward all-gas rather than settling low (the naive pace-to-echo failure).
    r = _ctrl_setup(mocker, target=100.0, best_echo=0.0, echo=100.0)
    seen = []
    for _ in range(4):
        r._maybe_adjust_rate()
        seen.append(r._target)
        r._echo_ewma = r._last_echo_rate = r._echo_ewma * 1.5   # echoes keep improving
    assert seen == sorted(seen) and seen[-1] > seen[0]         # monotonically climbing
    assert r._ctrl_climbing


def test_ctrl_ignored_when_not_replaying(mocker):
    r = _ctrl_setup(mocker, target=100.0)
    r.state = "testing"
    r._maybe_adjust_rate()
    assert r._target == 100.0                                  # untouched while not replaying
