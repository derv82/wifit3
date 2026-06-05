"""RTL88xxAU HalPwrSeqCmd runtime + the command/mask constants the tables use.

The per-chip power-sequence TABLES (rows transcribed from ``Hal8821APwrSeq.h`` /
``Hal8812PwrSeq.h``) live in each chip package; this base holds only the runtime that
walks them. It mirrors ``HalPwrSeqCmdParsing`` (``hal/HalPwrSeqCmd.c``): a row runs
only when its cut/fab/intf masks all intersect the chip's, WRITE is a read-modify-write
under ``msk``, POLLING reads until ``(v & msk) == (value & msk)``.

A chip package builds its flow as a list of rows
``(offset, cut_msk, fab_msk, intf_msk, base, cmd, msk, value)`` and drives it with the
right cut/fab/intf for the part (8821AU MP: cut=A; 8812AU NIC: PWR_CUT_ALL).
"""
from __future__ import annotations

import time

# Command codes [SRC] include/HalPwrSeqCmd.h:23-50
PWR_CMD_READ = 0x00
PWR_CMD_WRITE = 0x01
PWR_CMD_POLLING = 0x02
PWR_CMD_DELAY = 0x03
PWR_CMD_END = 0x04

# Base address selectors [SRC] :59-62
PWR_BASEADDR_MAC = 0x00
PWR_BASEADDR_SDIO = 0x03

# Interface / fab / cut masks [SRC] :67-90
PWR_INTF_SDIO_MSK = 0x01
PWR_INTF_USB_MSK = 0x02
PWR_INTF_PCI_MSK = 0x04
PWR_INTF_ALL_MSK = 0x0F
PWR_FAB_ALL_MSK = 0x0F
PWR_CUT_TESTCHIP_MSK = 0x01
PWR_CUT_A_MSK = 0x02
PWR_CUT_ALL_MSK = 0xFF

# Delay units [SRC] :93-96
PWRSEQ_DELAY_US = 0
PWRSEQ_DELAY_MS = 1

# Max polling iterations (USB) [SRC] HalPwrSeqCmd.c
POLL_MAX = 5000

# Bit shorthands matching the header's BIT0.. literals.
_B = [1 << n for n in range(8)]
B0, B1, B2, B3, B4, B5, B6, B7 = _B

# Row-field shorthands the per-chip tables reuse.
_MAC = PWR_BASEADDR_MAC
_SDIO = PWR_BASEADDR_SDIO
_ALL = PWR_INTF_ALL_MSK
_USB = PWR_INTF_USB_MSK
_USB_SDIO = PWR_INTF_USB_MSK | PWR_INTF_SDIO_MSK
_PCI = PWR_INTF_PCI_MSK
_SDIOI = PWR_INTF_SDIO_MSK
_CA = PWR_CUT_ALL_MSK
_FA = PWR_FAB_ALL_MSK


def hal_pwr_seq_cmd_parsing(t, cut, fab, intf, flow, delay=time.sleep):
    """Run a power-seq flow against transport *t*. Mirrors HalPwrSeqCmdParsing:
    filter by cut/fab/intf, then WRITE (read-modify-write), POLLING, or DELAY."""
    for offset, cut_msk, fab_msk, intf_msk, _base, cmd, msk, value in flow:
        if not (cut_msk & cut and fab_msk & fab and intf_msk & intf):
            continue
        if cmd == PWR_CMD_WRITE:
            cur = t.read8(offset)
            t.write8(offset, (cur & ~msk) | (value & msk))
        elif cmd == PWR_CMD_POLLING:
            for _ in range(POLL_MAX):
                if (t.read8(offset) & msk) == (value & msk):
                    break
                delay(10e-6)
            else:
                raise IOError(f"pwr-seq polling timeout @0x{offset:04x}")
        elif cmd == PWR_CMD_DELAY:
            delay(offset * (1e-3 if value == PWRSEQ_DELAY_MS else 1e-6))
        elif cmd == PWR_CMD_READ:
            t.read8(offset)
