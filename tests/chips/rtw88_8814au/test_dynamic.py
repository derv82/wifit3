"""Tests for the DIG (Dynamic Initial Gain) math — the no-link/coverage path of
rtw_phy_dig: walk IGI from the false-alarm count, clamped to [0x1c, 0x2a].

Uses a fake transport that records write32_mask calls (dig_write writes the IGI
to the 4 per-path registers); no USB/hardware.
"""
from __future__ import annotations

from wifit3.chips.rtw88_8814au import constants as C
from wifit3.chips.rtw88_8814au import dynamic


class FakeTransport:
    def __init__(self):
        self.writes = []  # (addr, mask, value)

    def write32_mask(self, addr, mask, value):
        self.writes.append((addr, mask, value))


def _igi_written(t):
    """The IGI value dig_write pushed (same to all 4 paths), or None."""
    if not t.writes:
        return None
    vals = {v for _, _, v in t.writes}
    assert len(vals) == 1, f"expected one IGI value, got {vals}"
    paths = {a for a, _, _ in t.writes}
    assert paths == set(C.REG_DIG_PATH)        # all 4 OFDM paths
    return vals.pop()


class TestDigStep:
    def test_low_fa_drifts_down_but_clamps_at_min(self):
        # pre=0x1c (min already); fa<2000 -> +0-2 -> 0x1a -> clamp 0x1c == pre -> no write.
        t = FakeTransport()
        st = dynamic.DigState(igi=C.DIG_CVRG_MIN, history=[C.DIG_CVRG_MIN] * 4)
        dynamic.dig_step(t, st, fa_cnt=100)
        assert _igi_written(t) is None             # unchanged, no write

    def test_low_fa_from_mid_drops_by_2(self):
        t = FakeTransport()
        st = dynamic.DigState(igi=0x20, history=[0x20] * 4)
        dynamic.dig_step(t, st, fa_cnt=100)        # <2000 -> 0x20-2
        assert _igi_written(t) == 0x1E

    def test_high_fa_raises(self):
        t = FakeTransport()
        st = dynamic.DigState(igi=0x20, history=[0x20] * 4)
        dynamic.dig_step(t, st, fa_cnt=6000)       # >5000 -> +4-2 -> 0x22
        assert _igi_written(t) == 0x22

    def test_mid_fa_steps(self):
        t = FakeTransport()
        st = dynamic.DigState(igi=0x20, history=[0x20] * 4)
        dynamic.dig_step(t, st, fa_cnt=3000)       # >2000 -> +2-2 -> 0x20 == pre -> no write
        assert _igi_written(t) is None

    def test_clamps_at_max(self):
        t = FakeTransport()
        st = dynamic.DigState(igi=C.DIG_CVRG_MAX, history=[C.DIG_CVRG_MAX] * 4)
        dynamic.dig_step(t, st, fa_cnt=9000)       # +4-2 -> 0x2c -> clamp 0x2a == pre -> no write
        assert _igi_written(t) is None

    def test_history_records_current(self):
        t = FakeTransport()
        st = dynamic.DigState(igi=0x20, history=[0x20] * 4)
        dynamic.dig_step(t, st, fa_cnt=6000)
        assert st.history[0] == 0x22               # newest first
        assert st.history[1] == 0x20
