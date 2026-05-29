"""`mt76x02_wait_for_txrx_idle` — polls MT_MAC_STATUS until TX+RX bits clear.

Kernel reference: mt76x02.h:252-258. Critical gate between mac_setaddr and
the WCID/SKEY reset loops — race-prevention for the inject-slot's state.
"""
import asyncio

import pytest

from wifit3.chips.mt76x2u import constants as C
from wifit3.chips.mt76x2u import mac


class FakeTransport:
    """Returns a sequence of MT_MAC_STATUS values on successive reads."""

    def __init__(self, sequence: list[int]):
        self.sequence = list(sequence)
        self.read_count = 0

    def read32(self, addr: int) -> int:
        assert addr == C.MT_MAC_STATUS, (
            f"wait_for_txrx_idle should only poll MAC_STATUS, got 0x{addr:x}"
        )
        self.read_count += 1
        # Stay at last value once sequence is exhausted (caller may poll past end).
        idx = min(self.read_count - 1, len(self.sequence) - 1)
        return self.sequence[idx]


@pytest.fixture
def event_loop():
    """pytest-asyncio uses this fixture name for the loop. Module-scoped
    so tests share one, since we sleep on a real loop."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


async def test_returns_true_when_already_idle():
    """First read returns 0 → both TX and RX bits clear → immediate True."""
    t = FakeTransport([0x00000000])
    ok = await mac.wait_for_txrx_idle(t)
    assert ok is True
    assert t.read_count == 1


async def test_returns_true_after_a_few_polls():
    """TX busy for 2 polls, then idle on the 3rd."""
    t = FakeTransport([
        C.MT_MAC_STATUS_TX,
        C.MT_MAC_STATUS_TX,
        0x00000000,
    ])
    ok = await mac.wait_for_txrx_idle(t)
    assert ok is True
    assert t.read_count == 3


async def test_returns_true_when_rx_drains_after_tx():
    """TX clears, RX still busy, then both clear."""
    t = FakeTransport([
        C.MT_MAC_STATUS_TX | C.MT_MAC_STATUS_RX,
        C.MT_MAC_STATUS_RX,
        0x00000000,
    ])
    ok = await mac.wait_for_txrx_idle(t)
    assert ok is True
    assert t.read_count == 3


async def test_returns_false_on_timeout():
    """Chip never drains → poll until deadline → return False. We don't
    assert a specific poll count because Windows asyncio sleep granularity
    is ~15 ms — what matters is that we polled at least twice (once at
    entry, once after a sleep) and returned False."""
    t = FakeTransport([C.MT_MAC_STATUS_TX | C.MT_MAC_STATUS_RX])
    ok = await mac.wait_for_txrx_idle(t, timeout_ms=20)
    assert ok is False
    assert t.read_count >= 2


async def test_ignores_non_txrx_bits():
    """Other bits in MAC_STATUS shouldn't block idleness — only TX+RX gate."""
    other_bits = 0xFFFFFFFF & ~(C.MT_MAC_STATUS_TX | C.MT_MAC_STATUS_RX)
    t = FakeTransport([other_bits])
    ok = await mac.wait_for_txrx_idle(t)
    assert ok is True


async def test_only_polls_mac_status_register():
    """Function must not touch any other register."""
    t = FakeTransport([0x00000000])
    await mac.wait_for_txrx_idle(t)
    # FakeTransport asserts on every read32 that addr == MT_MAC_STATUS;
    # just verifying that ran.
    assert t.read_count >= 1
