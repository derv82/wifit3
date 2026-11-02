"""Hardware-free regression for the RTL8188EUS (DKMS) silent-reset poll and its tick slot."""
import asyncio

import pytest

from wifit3.chips.rtl8188eus_dkms import dig, powertrack, sreset
from wifit3.chips.rtl8188eus_dkms import constants as C
from wifit3.chips.rtl8188eus_dkms.driver import Rtl8188eusDkmsDriver


class OpTx:
    """Register fake that records every op in wire order."""
    def __init__(self, regs):
        self.regs = dict(regs)
        self.ops = []

    def read32(self, a):
        self.ops.append(("R", a))
        return self.regs.get(a, 0)

    def read8(self, a):
        self.ops.append(("R", a))
        return self.regs.get(a, 0) & 0xFF

    def write32(self, a, v):
        self.ops.append(("W", a, v))


_HEALTHY_OPS = [("R", C.REG_TXDMA_STATUS), ("R", C.REG_RXDMA_STATUS), ("R", C.REG_FMETHR)]


@pytest.mark.parametrize("txdma", [0x00, C.TXDMA_STATUS_IF_GONE])
def test_status_check_no_reset_when_clean_or_gone(mocker, txdma):
    reset = mocker.patch.object(sreset, "sreset_reset")
    t = OpTx({C.REG_TXDMA_STATUS: txdma})
    sreset.status_check(t)
    reset.assert_not_called()
    assert t.ops == _HEALTHY_OPS


def test_status_check_wedged_txdma_resets_then_runs_linked_check(mocker):
    reset = mocker.patch.object(sreset, "sreset_reset")
    t = OpTx({C.REG_TXDMA_STATUS: 0x00000400})
    sreset.status_check(t)
    reset.assert_called_once_with()
    assert t.ops == _HEALTHY_OPS


def test_sreset_reset_emits_no_register_ops():
    t = OpTx({C.REG_TXDMA_STATUS: 0x00000400})
    sreset.status_check(t)
    assert t.ops == _HEALTHY_OPS


def test_status_check_writes_back_latched_rxdma():
    t = OpTx({C.REG_RXDMA_STATUS: 0x00000004})
    sreset.status_check(t)
    assert t.ops == [("R", C.REG_TXDMA_STATUS), ("R", C.REG_RXDMA_STATUS),
                     ("W", C.REG_RXDMA_STATUS, 0x00000004), ("R", C.REG_FMETHR)]


async def test_dig_watchdog_runs_dm_tick_after_wedged_txdma(monkeypatch):
    drv = Rtl8188eusDkmsDriver(OpTx({C.REG_TXDMA_STATUS: 0x00000400}))
    drv._watchdog_state = dig.WatchdogState(cur_ig_value=0x20, cur_cck_cca_thres=0x40)
    drv._pt_state = powertrack.seed_state(0, 0, 0x18, None, False)
    ticks = []

    def fake_watchdog_tick(t, state, pt_state):
        ticks.append(list(t.ops))
        raise asyncio.CancelledError

    monkeypatch.setattr(dig, "WATCHDOG_PERIOD_S", 0)
    monkeypatch.setattr(dig, "watchdog_tick", fake_watchdog_tick)
    await drv._dig_watchdog()
    assert ticks == [_HEALTHY_OPS]
