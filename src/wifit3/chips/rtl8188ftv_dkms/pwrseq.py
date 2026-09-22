"""RTL8188FTV DKMS power-sequence tables + parser (M3).

Ported from ``HalPwrSeqCmdParsing`` (hal/HalPwrSeqCmd.c:48-181) over the
``rtl8188F_card_enable_flow`` (hal/rtl8188f/Hal8188FPwrSeq.c:45-49, row macros
in include/Hal8188FPwrSeq.h:87-93,41-50,182-185). Rows are
``(offset, cut, fab, intf, base, cmd, mask, value)``; rows whose cut/fab/intf
masks miss the call's (e.g. the SDIO rows on USB) emit no wire ops.
"""
from __future__ import annotations

import time

from . import constants as C


def BIT(n: int) -> int:
    return 1 << n


CARD_ENABLE_FLOW: tuple = (
    (0x0086, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_SDIO_MSK,
     C.PWR_BASEADDR_SDIO, C.PWR_CMD_WRITE, BIT(0), 0),
    (0x0086, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_SDIO_MSK,
     C.PWR_BASEADDR_SDIO, C.PWR_CMD_POLLING, BIT(1), BIT(1)),
    (0x0005, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK,
     C.PWR_INTF_USB_MSK | C.PWR_INTF_SDIO_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(3) | BIT(4), 0),
    (0x00C4, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_USB_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(4), 0),
    (0x0005, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(2), 0),
    (0x0006, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_POLLING, BIT(1), BIT(1)),
    (0x0005, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(7), 0),
    (0x0005, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(3), 0),
    (0x0005, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(0), BIT(0)),
    (0x0005, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_POLLING, BIT(0), 0),
    (0x0027, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, 0xFF, 0x35),
    (0xFFFF, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     0, C.PWR_CMD_END, 0, 0),
)

ENTER_LPS_FLOW: tuple = (
    (0x0139, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(0), BIT(0)),
    (0x0522, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, 0xFF, 0xFF),
    (0x05F8, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_POLLING, 0xFF, 0),
    (0x05F9, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_POLLING, 0xFF, 0),
    (0x05FA, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_POLLING, 0xFF, 0),
    (0x0002, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(0), 0),
    (0x0002, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_DELAY, 0, C.PWRSEQ_DELAY_US),
    (0x0002, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(1), 0),
    (0x0100, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, 0xFF, 0x3F),
    (0x0101, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(1), 0),
    (0x0553, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(5), BIT(5)),
    (0xFFFF, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     0, C.PWR_CMD_END, 0, 0),
)

CARD_DISABLE_FLOW: tuple = (
    (0x001F, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, 0xFF, 0),
    (0x004E, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(7), 0),
    (0x0027, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, 0xFF, 0x34),
    (0x0005, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(1), BIT(1)),
    (0x0005, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_POLLING, BIT(1), 0),
    (0x0007, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_SDIO_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, 0xFF, 0x00),
    (0x0086, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_SDIO_MSK,
     C.PWR_BASEADDR_SDIO, C.PWR_CMD_WRITE, BIT(0), BIT(0)),
    (0x0086, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_SDIO_MSK,
     C.PWR_BASEADDR_SDIO, C.PWR_CMD_POLLING, BIT(1), 0),
    (0x0005, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK,
     C.PWR_INTF_USB_MSK | C.PWR_INTF_SDIO_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(3) | BIT(4), BIT(3)),
    (0x00C4, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_USB_MSK,
     C.PWR_BASEADDR_MAC, C.PWR_CMD_WRITE, BIT(4), BIT(4)),
    (0xFFFF, C.PWR_CUT_ALL_MSK, C.PWR_FAB_ALL_MSK, C.PWR_INTF_ALL_MSK,
     0, C.PWR_CMD_END, 0, 0),
)

_MAX_POLLING_COUNT = 5000


def parse(t, flow=CARD_ENABLE_FLOW,
          cut: int = C.PWR_CALL_CUT, fab: int = C.PWR_CALL_FAB,
          intf: int = C.PWR_CALL_INTF) -> bool:
    polling_count = 0
    for offset, cut_msk, fab_msk, intf_msk, _base, cmd, mask, value in flow:
        if not ((fab_msk & fab) and (cut_msk & cut) and (intf_msk & intf)):
            continue
        if cmd == C.PWR_CMD_READ:
            pass
        elif cmd == C.PWR_CMD_WRITE:
            current = t.read8(offset)
            t.write8(offset, (current & (~mask & 0xFF)) | (value & mask))
        elif cmd == C.PWR_CMD_POLLING:
            while True:
                if (t.read8(offset) & mask) == (value & mask):
                    break
                time.sleep(10e-6)
                polling_count += 1
                if polling_count > _MAX_POLLING_COUNT:
                    return False
        elif cmd == C.PWR_CMD_DELAY:
            time.sleep(offset * (1 if value == C.PWRSEQ_DELAY_US else 1000) / 1e6)
        elif cmd == C.PWR_CMD_END:
            return True
    return True
