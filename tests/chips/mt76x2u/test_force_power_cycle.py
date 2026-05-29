"""Tests for `force_power_cycle` — the hard WLAN-block off-then-on used to
recover from a wedged warm-reattach state where reset_wlan + power_on alone
don't clear retained MCU state (ROM patch bit, FCE config).
"""
import asyncio

import pytest

from wifit3.chips.mt76x2u import constants as C
from wifit3.chips.mt76x2u import power


class FakeTransport:
    def __init__(self, initial_reg: int = 0xFFFFFFFF):
        self.regs = {C.MT_WLAN_FUN_CTRL: initial_reg}
        self.writes: list[tuple[int, int]] = []

    def read32(self, addr: int) -> int:
        return self.regs.get(addr, 0)

    def write32(self, addr: int, value: int) -> None:
        value &= 0xFFFFFFFF
        self.regs[addr] = value
        self.writes.append((addr, value))


@pytest.mark.asyncio
async def test_force_power_cycle_clears_wlan_en_then_sets_it():
    """Two writes to WLAN_FUN_CTRL: first with EN+CLK_EN clear, second
    with EN+CLK_EN set."""
    # Start with all bits high (warm chip with everything enabled)
    t = FakeTransport(initial_reg=0xFFFFFFFF)
    await power.force_power_cycle(t)
    en_bits = C.MT_WLAN_FUN_CTRL_WLAN_EN | C.MT_WLAN_FUN_CTRL_WLAN_CLK_EN

    wlan_writes = [v for a, v in t.writes if a == C.MT_WLAN_FUN_CTRL]
    assert len(wlan_writes) == 2
    # First write should have EN+CLK_EN cleared
    assert (wlan_writes[0] & en_bits) == 0
    # Second write should have EN+CLK_EN set
    assert (wlan_writes[1] & en_bits) == en_bits


@pytest.mark.asyncio
async def test_force_power_cycle_preserves_other_bits():
    """Bits other than EN/CLK_EN should pass through unchanged on both
    writes (rmw-style: only the two power bits get toggled)."""
    # Pre-set FRC_WL_ANT_SEL + WLAN_RESET_RF bits as "noise" to check
    # they survive the toggle.
    other_bits = (
        C.MT_WLAN_FUN_CTRL_FRC_WL_ANT_SEL
        | C.MT_WLAN_FUN_CTRL_WLAN_RESET_RF
    )
    en_bits = C.MT_WLAN_FUN_CTRL_WLAN_EN | C.MT_WLAN_FUN_CTRL_WLAN_CLK_EN
    t = FakeTransport(initial_reg=other_bits | en_bits)
    await power.force_power_cycle(t)
    wlan_writes = [v for a, v in t.writes if a == C.MT_WLAN_FUN_CTRL]
    # Other bits should be preserved on both writes.
    for v in wlan_writes:
        assert (v & other_bits) == other_bits


@pytest.mark.asyncio
async def test_force_power_cycle_sleeps_between_off_and_on():
    """Cycle must wait between the off and on writes (so the chip's MCU
    actually power-drops). We verify the duration empirically: the call
    should take roughly 40 ms total (20 ms off + 20 ms on settle)."""
    t = FakeTransport(initial_reg=0xFFFFFFFF)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await power.force_power_cycle(t)
    elapsed = loop.time() - t0
    # Allow generous slack for asyncio scheduling jitter on Windows.
    assert elapsed >= 0.030, f"power cycle returned too fast: {elapsed * 1000:.1f}ms"


@pytest.mark.asyncio
async def test_force_power_cycle_only_writes_wlan_fun_ctrl():
    """The helper must not touch any other register — clean side-effects."""
    t = FakeTransport(initial_reg=0xFFFFFFFF)
    await power.force_power_cycle(t)
    addrs = {a for a, _ in t.writes}
    assert addrs == {C.MT_WLAN_FUN_CTRL}
