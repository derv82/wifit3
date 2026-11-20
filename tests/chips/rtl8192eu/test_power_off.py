"""rtl8192eu_power_off failure paths (8192e.c:1437-1626) against a mock transport."""
import pytest

from wifit3.chips.rtl8192eu import constants as C
from wifit3.chips.rtl8192eu.core import Rtl8xxxuPriv
from wifit3.chips.rtl8192eu.rtl8192e import (
    rtl8192eu_active_to_lps,
    rtl8192eu_fops,
    rtl8192eu_power_off,
)


class PollTransport:
    """REG_SCH_TX_CMD reads busy ``sch_busy_reads`` times; REG_APS_FSMCO+1 BIT(1) never clears."""

    def __init__(self, sch_busy_reads: int = 0) -> None:
        self.sch_busy_reads = sch_busy_reads
        self.sch_reads = 0
        self.writes: list[tuple[int, int]] = []

    def read8(self, addr: int) -> int:
        return C.BIT(1) if addr == C.REG_APS_FSMCO + 1 else 0

    def read16(self, addr: int) -> int:
        return 0

    def read32(self, addr: int) -> int:
        if addr == C.REG_SCH_TX_CMD:
            self.sch_reads += 1
            return 1 if self.sch_reads <= self.sch_busy_reads else 0
        return 0

    def write8(self, addr: int, val: int) -> int:
        self.writes.append((addr, val))
        return 1

    def write16(self, addr: int, val: int) -> int:
        self.writes.append((addr, val))
        return 2

    def write32(self, addr: int, val: int) -> int:
        self.writes.append((addr, val))
        return 4


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)


def test_power_off_carries_on_when_disabling_the_mac_times_out():
    # 8192e.c:1525-1530: active_to_emu warns and returns -EBUSY; power_off still runs emu_to_disabled.
    transport = PollTransport()
    rtl8192eu_power_off(Rtl8xxxuPriv(transport, rtl8192eu_fops))
    assert transport.writes[-1] == (C.REG_APS_FSMCO + 1, C.BIT(1) | C.BIT(3))


@pytest.mark.parametrize(("busy_reads", "resets"), [(99, True), (100, False), (101, True)])
def test_active_to_lps_follows_the_c_retry_count(busy_reads, resets):
    # 8192e.c:1452-1463: idle on the 101st poll leaves retry == 0 and skips the BB/MAC resets.
    transport = PollTransport(sch_busy_reads=busy_reads)
    rtl8192eu_active_to_lps(Rtl8xxxuPriv(transport, rtl8192eu_fops))
    assert transport.sch_reads == min(busy_reads + 1, 101)
    assert any(addr == C.REG_DUAL_TSF_RST for addr, _val in transport.writes) == resets
