"""RTL8188EUS HW power sequence — card power-on flow + parser.

Direct transcription of the vendor power tables and their runtime interpreter:
  [SRC] include/Hal8188EPwrSeq.h  (RTL8188E_TRANS_* macros)
  [SRC] hal/HalPwrSeqCmd.c        (HalPwrSeqCmdParsing)

``_InitPowerOn_8188EU`` runs ``Rtl8188E_NIC_PWR_ON_FLOW`` (= CARDEMU_TO_ACT, then
END) with cut=ALL, fab=ALL, intf=USB, then writes REG_CR. Each WRITE is an 8-bit
read-modify-write ``(v & ~msk) | (val & msk)``; each POLLING reads until
``(v & msk) == (val & msk)``. [WIRE] cap1 frames 169..143 (power-seq ops 6..40).
"""
from __future__ import annotations

import time
from typing import NamedTuple

from .constants import BIT, CR_ENABLE_BITS, REG_CR

# Command codes [SRC] HalPwrSeqCmd.h
PWR_CMD_WRITE = 0x01
PWR_CMD_POLLING = 0x02
PWR_CMD_DELAY = 0x03
PWR_CMD_END = 0x04

# Interface / cut masks [SRC] HalPwrSeqCmd.h
PWR_INTF_USB = BIT(1)
PWR_INTF_ALL = BIT(0) | BIT(1) | BIT(2) | BIT(3)
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
_ALL = PWR_INTF_ALL
_W = PWR_CMD_WRITE
_P = PWR_CMD_POLLING

# RTL8188E_TRANS_CARDEMU_TO_ACT [SRC] include/Hal8188EPwrSeq.h
_CARDEMU_TO_ACT = [
    PwrCfg(0x0006, _A, _ALL, _P, BIT(1), BIT(1)),          # wait 0x04[17]=1 power ready
    PwrCfg(0x0002, _A, _ALL, _W, BIT(0) | BIT(1), 0),      # 0x02[1:0]=0 reset BB
    PwrCfg(0x0026, _A, _ALL, _W, BIT(7), BIT(7)),          # 0x24[23] schmit trigger
    PwrCfg(0x0005, _A, _ALL, _W, BIT(7), 0),               # 0x04[15]=0 disable HWPDN
    PwrCfg(0x0005, _A, _ALL, _W, BIT(4) | BIT(3), 0),      # 0x04[12:11]=0 disable WL suspend
    PwrCfg(0x0005, _A, _ALL, _W, BIT(0), BIT(0)),          # 0x04[8]=1 release power state
    PwrCfg(0x0005, _A, _ALL, _P, BIT(0), 0),               # wait 0x04[8]=0
    PwrCfg(0x0023, _A, _ALL, _W, BIT(4), 0),               # LDO normal mode
]

# Rtl8188E_NIC_PWR_ON_FLOW = power_on_flow [SRC] Hal8188EPwrSeq.c:23
NIC_PWR_ON_FLOW = _CARDEMU_TO_ACT


def run_pwr_seq(t, table, cut: int = PWR_CUT_ALL, intf: int = PWR_INTF_USB) -> None:
    """Interpret one power-sequence table against transport ``t``.

    [SRC] HalPwrSeqCmdParsing. Only MAC-base commands occur in the power-on flow,
    so the SDIO base remapping is omitted. POLLING bounds match the vendor's
    ~5000-iteration cap (10 us between reads).
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
                    f"power-seq poll timeout at offset 0x{cmd.offset:04x}")
        elif cmd.cmd == PWR_CMD_DELAY:
            unit = 1e-6 if cmd.value == PWRSEQ_DELAY_US else 1e-3
            time.sleep(cmd.offset * unit)
        elif cmd.cmd == PWR_CMD_END:
            return


def power_on(t) -> None:
    """``_InitPowerOn_8188EU`` [SRC] usb/usb_halinit.c:124 — run the power-on flow,
    then enable MAC DMA/WMAC/SCHEDULE/SEC via REG_CR."""
    run_pwr_seq(t, NIC_PWR_ON_FLOW)
    # Enable MAC DMA/WMAC/SCHEDULE/SEC block (write16(REG_CR, 0) then the enable set).
    t.write16(REG_CR, 0x0000)        # suggested by zhouzhou: clear first
    v = t.read16(REG_CR)
    t.write16(REG_CR, v | CR_ENABLE_BITS)
