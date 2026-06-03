"""RTL8814AU HW power sequence — card-enable flow + parser.

Direct transcription of the vendor power tables and their runtime interpreter:
  [SRC] include/Hal8814PwrSeq.h  (RTL8814A_TRANS_* macros)
  [SRC] hal/HalPwrSeqCmd.c       (HalPwrSeqCmdParsing)

``_InitPowerOn_8814AU`` runs ``Rtl8814A_NIC_ENABLE_FLOW`` (= card-enable =
CARDDIS_TO_CARDEMU then CARDEMU_TO_ACT) with cut=~TESTCHIP, fab=ALL, intf=USB.
Each WRITE is an 8-bit read-modify-write ``(v & ~msk) | (val & msk)``; each
POLLING reads until ``(v & msk) == (val & msk)``. [WIRE] cap1 frames 5713..5781.
"""
from __future__ import annotations

import time
from typing import NamedTuple

from .constants import BIT

# Command codes [SRC] HalPwrSeqCmd.h
PWR_CMD_READ = 0x00
PWR_CMD_WRITE = 0x01
PWR_CMD_POLLING = 0x02
PWR_CMD_DELAY = 0x03
PWR_CMD_END = 0x04

# Interface / fab / cut masks [SRC] HalPwrSeqCmd.h
PWR_INTF_SDIO = BIT(0)
PWR_INTF_USB = BIT(1)
PWR_INTF_PCI = BIT(2)
PWR_INTF_ALL = BIT(0) | BIT(1) | BIT(2) | BIT(3)
PWR_FAB_ALL = BIT(0) | BIT(1) | BIT(2) | BIT(3)
PWR_CUT_TESTCHIP = BIT(0)
PWR_CUT_ALL = 0xFF

PWRSEQ_DELAY_US = 0
PWRSEQ_DELAY_MS = 1


class PwrCfg(NamedTuple):
    offset: int
    cut_msk: int
    intf_msk: int
    cmd: int
    msk: int
    value: int


_A = PWR_CUT_ALL
_T = PWR_CUT_TESTCHIP
_ALL = PWR_INTF_ALL
_USB = PWR_INTF_USB
_PCI = PWR_INTF_PCI
_W = PWR_CMD_WRITE
_P = PWR_CMD_POLLING
_D = PWR_CMD_DELAY

# RTL8814A_TRANS_CARDDIS_TO_CARDEMU [SRC] Hal8814PwrSeq.h:151
_CARDDIS_TO_CARDEMU = [
    PwrCfg(0x0012, _A, _ALL, _W, BIT(6), BIT(6)),  # 0x12[6]=1 force PWM mode
    PwrCfg(0x0015, _A, _ALL, _W, BIT(5), 0),       # 0x15[5]=0 turn off ZCD
    PwrCfg(0x0015, _A, _ALL, _W, BIT(6), 0),       # 0x15[6]=0 ZCD output off
    PwrCfg(0x0023, _A, _ALL, _W, BIT(4), 0),       # 0x23[4]=0 hpon LDO leave sleep
    PwrCfg(0x0046, _A, _ALL, _W, 0xFF, 0x00),      # gpio0~7 input mode
    PwrCfg(0x0062, _A, _ALL, _W, 0xFF, 0x00),      # gpio11..8 input mode
    PwrCfg(0x0005, _A, _PCI, _W, BIT(2), 0),       # (PCI only) enable SW LPS
    PwrCfg(0x0005, _A, _ALL, _W, BIT(3), 0),       # 0x04[11]=0 enable WL suspend
    PwrCfg(0x0301, _A, _PCI, _W, 0xFF, 0),         # (PCI only) PCIe DMA start
    PwrCfg(0x0071, _A, _PCI, _W, BIT(2), 0),       # (PCI only) CPHY_MBIAS_EN off
]

# RTL8814A_TRANS_CARDEMU_TO_ACT [SRC] Hal8814PwrSeq.h:54
_CARDEMU_TO_ACT = [
    PwrCfg(0x0005, _A, _ALL, _W, BIT(2), 0),        # disable SW LPS 0x04[10]=0
    PwrCfg(0x0006, _A, _ALL, _P, BIT(1), BIT(1)),   # wait 0x04[17]=1 power ready
    PwrCfg(0x002B, _T, _ALL, _W, BIT(0), BIT(0)),   # (testchip) pll phase select
    PwrCfg(0x0015, _T, _ALL, _W, BIT(3) | BIT(2) | BIT(1), BIT(3) | BIT(2) | BIT(1)),
    PwrCfg(0x002D, _T, _ALL, _W, 0x0E, 0x08),       # (testchip) lpf R3
    PwrCfg(0x002D, _T, _ALL, _W, 0x70, 0x50),       # (testchip) lpf Rs
    PwrCfg(0x007B, _T, _ALL, _W, BIT(6), BIT(6)),   # (testchip) SDM order select
    PwrCfg(0x0005, _A, _ALL, _W, BIT(3), 0),        # disable WL suspend
    PwrCfg(0x00F0, _A, _ALL, _W, BIT(7), 0),
    PwrCfg(0x0081, _A, _ALL, _W, 0x30, 0x20),
    PwrCfg(0x0005, _A, _ALL, _W, BIT(0), BIT(0)),   # 0x04[8]=1 release power state
    PwrCfg(0x0005, _A, _ALL, _P, BIT(0), 0),        # poll until 0x04[8]=0
]

# Rtl8814A_NIC_ENABLE_FLOW = card_enable_flow [SRC] Hal8814PwrSeq.c:42
NIC_ENABLE_FLOW = _CARDDIS_TO_CARDEMU + _CARDEMU_TO_ACT


def run_pwr_seq(t, table, cut: int, intf: int) -> None:
    """Interpret one power-sequence table against transport ``t``.

    [SRC] HalPwrSeqCmdParsing. Only MAC-base commands occur in the card-enable
    flow, so the SDIO/GSPI offset remapping is omitted. POLLING bounds match the
    vendor's 5000-iteration cap (10 us between reads).
    """
    for cmd in table:
        if not (cmd.cut_msk & cut) or not (cmd.intf_msk & intf):
            continue
        if cmd.cmd == PWR_CMD_WRITE:
            v = t.read8(cmd.offset)
            v = (v & ~cmd.msk) | (cmd.value & cmd.msk)
            t.write8(cmd.offset, v & 0xFF)
        elif cmd.cmd == PWR_CMD_POLLING:
            target = cmd.value & cmd.msk
            for _ in range(5000):
                if (t.read8(cmd.offset) & cmd.msk) == target:
                    break
                time.sleep(10e-6)
            else:
                raise RuntimeError(
                    f"power-seq poll timeout at offset 0x{cmd.offset:04x}"
                )
        elif cmd.cmd == PWR_CMD_DELAY:
            # offset = count; value = unit (0:us, 1:ms)
            unit = 1e-6 if cmd.value == PWRSEQ_DELAY_US else 1e-3
            time.sleep(cmd.offset * unit)
        elif cmd.cmd == PWR_CMD_END:
            return
