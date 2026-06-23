"""Tests for the RTL8822BU DIG (Dynamic Initial Gain) port — the no-link /
coverage path of rtw_phy_dig: walk IGI from the false-alarm count, clamped to
[0x1c, 0x2a], written to both OFDM paths (0xc50/0xe50).

Also covers the kernel seed (dig_init reads dig[0], no write) and the
8822b false-alarm accounting (ofdm + cck-if-enabled, then counter reset). A fake
transport stands in for USB — no hardware.
"""
from __future__ import annotations

from wifit3.chips.rtl8822bu import dynamic


class FakeTransport:
    """Records register access; serves canned values for reads by address."""

    def __init__(self, reads: dict[int, int] | None = None):
        self._reads = reads or {}
        self.mask_writes: list[tuple[int, int, int]] = []  # (addr, mask, value)
        self.bit_ops: list[tuple[str, int, int]] = []      # (op, addr, mask)

    def read32(self, addr):
        return self._reads.get(addr, 0)

    def read16(self, addr):
        return self._reads.get(addr, 0)

    def write32_mask(self, addr, mask, value):
        self.mask_writes.append((addr, mask, value))

    def write32_set(self, addr, mask):
        self.bit_ops.append(("set", addr, mask))

    def write32_clr(self, addr, mask):
        self.bit_ops.append(("clr", addr, mask))


def _igi_written(t):
    """The IGI value dig_write pushed (same to both paths), or None."""
    if not t.mask_writes:
        return None
    vals = {v for _, _, v in t.mask_writes}
    assert len(vals) == 1, f"expected one IGI value, got {vals}"
    paths = {a for a, _, _ in t.mask_writes}
    assert paths == set(dynamic.REG_DIG_PATH)   # both OFDM paths (0xc50/0xe50)
    masks = {m for _, m, _ in t.mask_writes}
    assert masks == {dynamic.DIG_IGI_MASK}
    return vals.pop()


class TestDigStep:
    def test_low_fa_drifts_down_but_clamps_at_min(self):
        # pre=0x1c (already min); fa<2000 -> -2 -> 0x1a -> clamp 0x1c == pre.
        t = FakeTransport()
        st = dynamic.DigState(igi=dynamic.DIG_CVRG_MIN,
                              history=[dynamic.DIG_CVRG_MIN] * 4)
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

    def test_mid_fa_no_net_change(self):
        t = FakeTransport()
        st = dynamic.DigState(igi=0x20, history=[0x20] * 4)
        dynamic.dig_step(t, st, fa_cnt=3000)       # >2000 -> +2-2 -> 0x20 == pre
        assert _igi_written(t) is None

    def test_clamps_at_max(self):
        t = FakeTransport()
        st = dynamic.DigState(igi=dynamic.DIG_CVRG_MAX,
                              history=[dynamic.DIG_CVRG_MAX] * 4)
        dynamic.dig_step(t, st, fa_cnt=9000)       # +4-2 -> 0x2c -> clamp 0x2a == pre
        assert _igi_written(t) is None

    def test_high_default_pulled_into_range(self):
        # A seed can read an AGC default above the coverage band; the
        # first tick must pull it down to the 0x2a ceiling (fixes saturation).
        t = FakeTransport()
        st = dynamic.DigState(igi=0x30, history=[0x30] * 4)
        dynamic.dig_step(t, st, fa_cnt=100)        # 0x30-2=0x2e -> clamp 0x2a
        assert _igi_written(t) == 0x2A

    def test_history_records_current(self):
        t = FakeTransport()
        st = dynamic.DigState(igi=0x20, history=[0x20] * 4)
        dynamic.dig_step(t, st, fa_cnt=6000)
        assert st.history[0] == 0x22               # newest first
        assert st.history[1] == 0x20


class TestDigInit:
    def test_seeds_from_agc_default_without_writing(self):
        # Kernel rtw_phy_init seeds igi_history[0] = read(dig[0]); no IGI write.
        t = FakeTransport(reads={dynamic.REG_DIG_PATH[0]: 0x1234_5A00 | 0x24})
        st = dynamic.dig_init(t)
        assert st.igi == 0x24                       # 0x...24 & 0x7f
        assert st.history == [0x24] * 4
        assert t.mask_writes == []                  # read-only seed


class TestFalseAlarm:
    def test_total_fa_ofdm_only_when_cck_disabled(self):
        t = FakeTransport(reads={
            dynamic.REG_CCK_DEMOD: 0,                # BIT(28) clear -> CCK off
            dynamic.REG_FA_CCK: 500,
            dynamic.REG_FA_OFDM: 1200,
        })
        assert dynamic.read_total_fa_cnt(t) == 1200  # cck excluded

    def test_total_fa_includes_cck_when_enabled(self):
        t = FakeTransport(reads={
            dynamic.REG_CCK_DEMOD: dynamic.BIT_CCK_EN,
            dynamic.REG_FA_CCK: 500,
            dynamic.REG_FA_OFDM: 1200,
        })
        assert dynamic.read_total_fa_cnt(t) == 1700

    def test_counter_reset_sequence(self):
        # Mirror rtw8822b.c:1063-1068 exactly, including per-register set/clr order.
        t = FakeTransport()
        dynamic.read_total_fa_cnt(t)
        assert t.bit_ops == [
            ("set", 0x9A4, 1 << 17),
            ("clr", 0x9A4, 1 << 17),
            ("clr", 0xA2C, 1 << 15),
            ("set", 0xA2C, 1 << 15),
            ("set", 0xB58, 1 << 0),
            ("clr", 0xB58, 1 << 0),
        ]
