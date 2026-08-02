"""Power-on / power-off sequences for RTL8812AU.

Direct port of the `rtw_pwr_seq_cmd` tables from
`driver_sources/rtw88-source-v6.18/rtw8812a_table.c` (lines 2259..2613).
Each tuple maps 1:1 to a C struct entry so the tables can be audited
against upstream.

Semantics (mirroring `rtw_sub_pwr_seq_parser` in `mac.c:185`):
- WRITE   : value = (read8(addr) & ~mask) | (cmd_value & mask); write8(addr, value)
- POLLING : poll until (read8(addr) & mask) == (cmd_value & mask)
- DELAY   : if cmd_value == DELAY_US: usleep(addr) else: msleep(addr)
- END     : terminate sub-sequence

We only filter by interface mask (we always run with INTF_USB). `cut_mask`
is `RTW_PWR_CUT_ALL_MSK` (0xFF) on every 8812A entry, so we don't need to
detect the silicon cut version for the power-on flow.
"""

from __future__ import annotations

import logging
from typing import Sequence

from wifit3.chips.rtw88_base.power_seq import (
    ADDR_MAC,
    ADDR_SDIO,
    CMD_DELAY,
    CMD_END,
    CMD_POLLING,
    CMD_READ,
    CMD_WRITE,
    CUT_ALL,
    DELAY_MS,
    DELAY_US,
    INTF_ALL,
    INTF_PCI,
    INTF_SDIO,
    INTF_USB,
    run_pwr_flow,
    run_pwr_seq,
)

from .transport import RTL8812AUTransport

logger = logging.getLogger(__name__)


# Each tuple = (offset, cut_mask, intf_mask, base, cmd, mask, value) — matches
# the C struct `rtw_pwr_seq_cmd` field order exactly.

# rtw8812a_table.c:2259 — trans_carddis_to_cardemu_8812a
CARDDIS_TO_CARDEMU = (
    (0x0012, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   1 << 0,              1 << 0),
    (0x0014, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   0x80,                0),
    (0x0015, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   0x01,                0),
    (0x0023, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   0x10,                0),
    (0x0046, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   0xFF,                0x00),
    (0x0043, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   0xFF,                0x00),
    (0x0005, CUT_ALL, INTF_PCI, ADDR_MAC, CMD_WRITE,   1 << 2,              0),
    (0x0005, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   1 << 3,              0),
    (0x0003, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   1 << 2,              1 << 2),
    (0x0301, CUT_ALL, INTF_PCI, ADDR_MAC, CMD_WRITE,   0xFF,                0),
    (0x0024, CUT_ALL, INTF_USB, ADDR_MAC, CMD_WRITE,   1 << 1,              1 << 1),
    (0x0028, CUT_ALL, INTF_USB, ADDR_MAC, CMD_WRITE,   1 << 3,              1 << 3),
    (0xFFFF, CUT_ALL, INTF_ALL, 0,        CMD_END,     0,                   0),
)

# rtw8812a_table.c:2327 — trans_cardemu_to_act_8812a
CARDEMU_TO_ACT = (
    (0x0005, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   1 << 2,              0),
    (0x0006, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_POLLING, 1 << 1,              1 << 1),
    (0x0005, CUT_ALL, INTF_PCI, ADDR_MAC, CMD_WRITE,   1 << 7,              0),
    (0x0005, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   1 << 3,              0),
    (0x0005, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   1 << 0,              1 << 0),
    (0x0005, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_POLLING, 1 << 0,              0),
    (0x0024, CUT_ALL, INTF_USB, ADDR_MAC, CMD_WRITE,   1 << 1,              0),
    (0x0028, CUT_ALL, INTF_USB, ADDR_MAC, CMD_WRITE,   1 << 3,              0),
    (0xFFFF, CUT_ALL, INTF_ALL, 0,        CMD_END,     0,                   0),
)

# Top-level flow (rtw8812a_table.c:2599)
CARD_ENABLE_FLOW_8812A: Sequence[Sequence[Sequence[int]]] = (
    CARDDIS_TO_CARDEMU,
    CARDEMU_TO_ACT,
)


# rtw8812a_table.c:2453 — trans_act_to_cardemu_8812a
ACT_TO_CARDEMU = (
    (0x0C00, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   0xFF,                0x04),
    (0x0E00, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   0xFF,                0x04),
    (0x0002, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   1 << 0,              0),
    (0x0002, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_DELAY,   0,                   DELAY_US),
    (0x0002, CUT_ALL, INTF_PCI, ADDR_MAC, CMD_WRITE,   1 << 1,              0),
    (0x0007, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   0xFF,                0x2A),
    (0x0008, CUT_ALL, INTF_USB, ADDR_MAC, CMD_WRITE,   0x02,                0),
    (0x0005, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_WRITE,   1 << 1,              1 << 1),
    (0x0005, CUT_ALL, INTF_ALL, ADDR_MAC, CMD_POLLING, 1 << 1,              0),
    (0xFFFF, CUT_ALL, INTF_ALL, 0,        CMD_END,     0,                   0),
)

# rtw8812a_table.c:2506 — trans_cardemu_to_carddis_8812a
CARDEMU_TO_CARDDIS = (
    (0x0003, CUT_ALL, INTF_ALL, ADDR_MAC,  CMD_WRITE,   1 << 2,              0),
    (0x0080, CUT_ALL, INTF_ALL, ADDR_MAC,  CMD_WRITE,   0xFF,                0x05),
    (0x0042, CUT_ALL, INTF_USB, ADDR_MAC,  CMD_WRITE,   0xF0,                0xCC),
    (0x0042, CUT_ALL, INTF_PCI, ADDR_MAC,  CMD_WRITE,   0xF0,                0xEC),
    (0x0043, CUT_ALL, INTF_ALL, ADDR_MAC,  CMD_WRITE,   0xFF,                0x07),
    (0x0045, CUT_ALL, INTF_ALL, ADDR_MAC,  CMD_WRITE,   0xFF,                0x00),
    (0x0046, CUT_ALL, INTF_ALL, ADDR_MAC,  CMD_WRITE,   0xFF,                0xFF),
    (0x0047, CUT_ALL, INTF_ALL, ADDR_MAC,  CMD_WRITE,   0xFF,                0),
    (0x0014, CUT_ALL, INTF_ALL, ADDR_MAC,  CMD_WRITE,   0x80,                1 << 7),
    (0x0015, CUT_ALL, INTF_ALL, ADDR_MAC,  CMD_WRITE,   0x01,                1 << 0),
    (0x0012, CUT_ALL, INTF_ALL, ADDR_MAC,  CMD_WRITE,   0x01,                0),
    (0x0023, CUT_ALL, INTF_ALL, ADDR_MAC,  CMD_WRITE,   0x10,                1 << 4),
    (0x0008, CUT_ALL, INTF_USB, ADDR_MAC,  CMD_WRITE,   0x02,                0),
    (0x0007, CUT_ALL, INTF_USB, ADDR_MAC,  CMD_WRITE,   0xFF,                0x20),
    (0x001F, CUT_ALL, INTF_USB, ADDR_MAC,  CMD_WRITE,   1 << 1,              0),
    (0x0076, CUT_ALL, INTF_USB, ADDR_MAC,  CMD_WRITE,   1 << 1,              0),
    (0x0005, CUT_ALL, INTF_ALL, ADDR_MAC,  CMD_WRITE,   1 << 3,              1 << 3),
    (0xFFFF, CUT_ALL, INTF_ALL, 0,         CMD_END,     0,                   0),
)

# Top-level flow (rtw8812a_table.c:2610)
CARD_DISABLE_FLOW_8812A: Sequence[Sequence[Sequence[int]]] = (
    ACT_TO_CARDEMU,
    CARDEMU_TO_CARDDIS,
)


def card_enable_flow_8812a(transport: RTL8812AUTransport) -> None:
    """Run the full power-on sequence for the 8812A on a USB host."""
    run_pwr_flow(transport, CARD_ENABLE_FLOW_8812A,
                 intf_mask=INTF_USB, cut_mask=CUT_ALL)


def card_disable_flow_8812a(transport: RTL8812AUTransport) -> None:
    """Run the full power-off sequence for the 8812A on a USB host."""
    run_pwr_flow(transport, CARD_DISABLE_FLOW_8812A,
                 intf_mask=INTF_USB, cut_mask=CUT_ALL)


__all__ = [
    "ADDR_MAC", "ADDR_SDIO",
    "CMD_DELAY", "CMD_END", "CMD_POLLING", "CMD_READ", "CMD_WRITE",
    "CUT_ALL", "DELAY_MS", "DELAY_US",
    "INTF_ALL", "INTF_PCI", "INTF_SDIO", "INTF_USB",
    "CARDDIS_TO_CARDEMU", "CARDEMU_TO_ACT",
    "CARD_ENABLE_FLOW_8812A", "CARD_DISABLE_FLOW_8812A",
    "ACT_TO_CARDEMU", "CARDEMU_TO_CARDDIS",
    "card_enable_flow_8812a", "card_disable_flow_8812a",
    "run_pwr_seq", "run_pwr_flow",
]
