"""Tests for WEP ARP replay: re-addressing, patient testing, adaptive burst.

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
    return WepArpReplay(iface, ap, collector, rx_window=0.0, **kw)


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


async def test_can_inject_gate_blocks_tx(mocker):
    coll = FakeCollector({0xAA: 5}, [GOOD])
    r = _replay(mocker, coll, can_inject=lambda: False)
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


def test_adapt_grows_on_throughput(mocker):
    r = _replay(mocker, FakeCollector({0xAA: 5}, [GOOD]))
    base = r._burst_size
    r._adapt(10)
    assert r._burst_size > base


def test_adapt_is_sticky_backs_off_only_after_sustained_drop(mocker):
    r = _replay(mocker, FakeCollector({0xAA: 5}, [GOOD]))
    r._adapt(10)
    grown = r._burst_size
    r._adapt(0)
    assert r._burst_size == grown
    r._adapt(0)
    assert r._burst_size == grown
    r._adapt(0)
    assert r._burst_size < grown
    assert r._burst_size >= r._MIN_BURST
