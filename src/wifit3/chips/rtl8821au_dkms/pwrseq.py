"""RTL8821A power sequences + HalPwrSeqCmd runtime.

Rows are transcribed verbatim from `include/Hal8821APwrSeq.h` as
(offset, cut_msk, fab_msk, intf_msk, base, cmd, msk, value); the flows are the
same concatenations the vendor builds in `hal/rtl8812a/Hal8821APwrSeq.c`. The
runtime mirrors `HalPwrSeqCmdParsing` (`hal/HalPwrSeqCmd.c`): a row runs only when
cut/fab/intf masks all intersect the chip's, WRITE is a read-modify-write under
`msk`, POLLING reads until `(v & msk) == (value & msk)`.

For the AWUS036ACS bring-up the chip is the 8821A MP chip on USB, so the card is
driven with cut=CUT_A, fab=ALL, intf=USB — only USB+cut-A rows execute
(SDIO/PCI/test-chip rows fall out).  # TODO(8812au): the 8812 path runs
Rtl8812_NIC_ENABLE_FLOW with PWR_CUT_ALL.
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

_MAC = PWR_BASEADDR_MAC
_SDIO = PWR_BASEADDR_SDIO
_ALL = PWR_INTF_ALL_MSK
_USB = PWR_INTF_USB_MSK
_USB_SDIO = PWR_INTF_USB_MSK | PWR_INTF_SDIO_MSK
_PCI = PWR_INTF_PCI_MSK
_SDIOI = PWR_INTF_SDIO_MSK
_CA = PWR_CUT_ALL_MSK
_FA = PWR_FAB_ALL_MSK

# [SRC] include/Hal8821APwrSeq.h:126-135
RTL8821A_TRANS_CARDDIS_TO_CARDEMU = [
    (0x0005, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B3 | B7, 0),
    (0x0086, _CA, _FA, _SDIOI, _SDIO, PWR_CMD_WRITE, B0, 0),
    (0x0086, _CA, _FA, _SDIOI, _SDIO, PWR_CMD_POLLING, B1, B1),
    (0x004A, _CA, _FA, _USB, _MAC, PWR_CMD_WRITE, B0, 0),
    (0x0005, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B3 | B4, 0),
    (0x0023, _CA, _FA, _SDIOI, _MAC, PWR_CMD_WRITE, B4, 0),
    (0x0301, _CA, _FA, _PCI, _MAC, PWR_CMD_WRITE, 0xFF, 0),
]

# [SRC] include/Hal8821APwrSeq.h:53-79
RTL8821A_TRANS_CARDEMU_TO_ACT = [
    (0x0020, _CA, _FA, _USB_SDIO, _MAC, PWR_CMD_WRITE, B0, B0),
    (0x0067, _CA, _FA, _USB_SDIO, _MAC, PWR_CMD_WRITE, B4, 0),
    (0x0001, _CA, _FA, _USB_SDIO, _MAC, PWR_CMD_DELAY, 1, PWRSEQ_DELAY_MS),
    (0x0000, _CA, _FA, _USB_SDIO, _MAC, PWR_CMD_WRITE, B5, 0),
    (0x0005, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B4 | B3 | B2, 0),
    (0x0075, _CA, _FA, _PCI, _MAC, PWR_CMD_WRITE, B0, B0),
    (0x0006, _CA, _FA, _ALL, _MAC, PWR_CMD_POLLING, B1, B1),
    (0x0075, _CA, _FA, _PCI, _MAC, PWR_CMD_WRITE, B0, 0),
    (0x0006, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B0, B0),
    (0x0005, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B7, 0),
    (0x0005, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B4 | B3, 0),
    (0x0005, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B0, B0),
    (0x0005, _CA, _FA, _ALL, _MAC, PWR_CMD_POLLING, B0, 0),
    (0x004F, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B0, B0),
    (0x0067, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B5 | B4, B5 | B4),
    (0x0025, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B6, 0),
    (0x0049, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B1, B1),
    (0x0063, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B1, B1),
    (0x0062, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B1, 0),
    (0x0058, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B0, B0),
    (0x005A, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B1, B1),
    (0x007A, PWR_CUT_TESTCHIP_MSK, _FA, _ALL, _MAC, PWR_CMD_WRITE, 0xFF, 0x3A),
    (0x002E, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, 0xFF, 0x82),
    (0x0010, PWR_CUT_A_MSK, _FA, _ALL, _MAC, PWR_CMD_WRITE, B6, B6),
]

# [SRC] include/Hal8821APwrSeq.h:85-92
RTL8821A_TRANS_ACT_TO_CARDEMU = [
    (0x001F, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, 0xFF, 0),
    (0x004F, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B0, 0),
    (0x0049, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B1, 0),
    (0x0006, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B0, B0),
    (0x0005, _CA, _FA, _ALL, _MAC, PWR_CMD_WRITE, B1, B1),
    (0x0005, _CA, _FA, _ALL, _MAC, PWR_CMD_POLLING, B1, 0),
    (0x0000, _CA, _FA, _USB_SDIO, _MAC, PWR_CMD_WRITE, B5, B5),
    (0x0020, _CA, _FA, _USB_SDIO, _MAC, PWR_CMD_WRITE, B0, 0),
]

# [SRC] include/Hal8821APwrSeq.h:118-124
RTL8821A_TRANS_CARDEMU_TO_CARDDIS = [
    (0x0007, _CA, _FA, _SDIOI, _MAC, PWR_CMD_WRITE, 0xFF, 0x20),
    (0x0005, _CA, _FA, _USB_SDIO, _MAC, PWR_CMD_WRITE, B3 | B4, B3),
    (0x0005, _CA, _FA, _PCI, _MAC, PWR_CMD_WRITE, B2, B2),
    (0x004A, _CA, _FA, _USB, _MAC, PWR_CMD_WRITE, B0, 1),
    (0x0023, _CA, _FA, _SDIOI, _MAC, PWR_CMD_WRITE, B4, B4),
    (0x0086, _CA, _FA, _SDIOI, _SDIO, PWR_CMD_WRITE, B0, B0),
    (0x0086, _CA, _FA, _SDIOI, _SDIO, PWR_CMD_POLLING, B1, 0),
]

# Flows [SRC] hal/rtl8812a/Hal8821APwrSeq.c:38-49
CARD_ENABLE_FLOW = RTL8821A_TRANS_CARDDIS_TO_CARDEMU + RTL8821A_TRANS_CARDEMU_TO_ACT
CARD_DISABLE_FLOW = RTL8821A_TRANS_ACT_TO_CARDEMU + RTL8821A_TRANS_CARDEMU_TO_CARDDIS


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
