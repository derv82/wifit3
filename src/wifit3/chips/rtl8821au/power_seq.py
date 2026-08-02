"""Power-on / power-off sequences for RTL8821AU.

Direct translation of the rtw_pwr_seq_cmd tables from
`driver_sources/rtw88-source-v6.18/rtw8821a_table.c`. Each tuple maps 1:1
to a C struct entry so the tables can be audited against upstream.

Semantics (mirroring `rtw_sub_pwr_seq_parser` in `mac.c:185`):
- WRITE   : value = (read8(addr) & ~mask) | (cmd_value & mask); write8(addr, value)
- POLLING : poll until (read8(addr) & mask) == (cmd_value & mask)
- DELAY   : if cmd_value == DELAY_US: usleep(addr) else: msleep(addr)
- END     : terminate sub-sequence

We only filter by interface mask. `cut_mask` is `RTW_PWR_CUT_ALL_MSK`
(0xFF) on every 8821A entry, so we don't need to detect the silicon
cut version for the power-on flow.
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

from .transport import RTL8821AUTransport

logger = logging.getLogger(__name__)


# Each tuple = (offset, cut_mask, intf_mask, base, cmd, mask, value) — matches
# the C struct `rtw_pwr_seq_cmd` field order exactly.

# rtw8821a_table.c:1906 — trans_carddis_to_cardemu_8821a
CARDDIS_TO_CARDEMU = (
    (0x0005, CUT_ALL, INTF_ALL,  ADDR_MAC,  CMD_WRITE,   (1 << 3) | (1 << 7), 0),
    (0x0086, CUT_ALL, INTF_SDIO, ADDR_SDIO, CMD_WRITE,   1 << 0,              0),
    (0x0086, CUT_ALL, INTF_SDIO, ADDR_SDIO, CMD_POLLING, 1 << 1,              1 << 1),
    (0x004A, CUT_ALL, INTF_USB,  ADDR_MAC,  CMD_WRITE,   1 << 0,              0),
    (0x0005, CUT_ALL, INTF_ALL,  ADDR_MAC,  CMD_WRITE,   (1 << 3) | (1 << 4), 0),
    (0x0023, CUT_ALL, INTF_SDIO, ADDR_MAC,  CMD_WRITE,   1 << 4,              0),
    (0x0301, CUT_ALL, INTF_PCI,  ADDR_MAC,  CMD_WRITE,   0xFF,                0),
    (0xFFFF, CUT_ALL, INTF_ALL,  0,         CMD_END,     0,                   0),
)

# rtw8821a_table.c:1949 — trans_cardemu_to_act_8821a
CARDEMU_TO_ACT = (
    (0x0020, CUT_ALL, INTF_USB | INTF_SDIO, ADDR_MAC, CMD_WRITE,   1 << 0, 1 << 0),
    (0x0067, CUT_ALL, INTF_USB | INTF_SDIO, ADDR_MAC, CMD_WRITE,   1 << 4, 0),
    (0x0001, CUT_ALL, INTF_USB | INTF_SDIO, ADDR_MAC, CMD_DELAY,   1, DELAY_MS),
    (0x0000, CUT_ALL, INTF_USB | INTF_SDIO, ADDR_MAC, CMD_WRITE,   1 << 5, 0),
    (0x0005, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   (1 << 4) | (1 << 3) | (1 << 2), 0),
    (0x0075, CUT_ALL, INTF_PCI,             ADDR_MAC, CMD_WRITE,   1 << 0, 1 << 0),
    (0x0006, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_POLLING, 1 << 1, 1 << 1),
    (0x0075, CUT_ALL, INTF_PCI,             ADDR_MAC, CMD_WRITE,   1 << 0, 0),
    (0x0006, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 0, 1 << 0),
    (0x0005, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 7, 0),
    (0x0005, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   (1 << 4) | (1 << 3), 0),
    (0x0005, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 0, 1 << 0),
    (0x0005, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_POLLING, 1 << 0, 0),
    (0x004F, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 0, 1 << 0),
    (0x0067, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   (1 << 5) | (1 << 4), (1 << 5) | (1 << 4)),
    (0x0025, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 6, 0),
    (0x0049, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 1, 1 << 1),
    (0x0063, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 1, 1 << 1),
    (0x0062, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 1, 0),
    (0x0058, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 0, 1 << 0),
    (0x005A, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 1, 1 << 1),
    (0x002E, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   0xFF, 0x82),
    (0x0010, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 6, 1 << 6),
    (0xFFFF, CUT_ALL, INTF_ALL,             0,        CMD_END,     0, 0),
)

# Top-level flow (rtw8821a_table.c:2236)
CARD_ENABLE_FLOW_8821A: Sequence[Sequence[Sequence[int]]] = (
    CARDDIS_TO_CARDEMU,
    CARDEMU_TO_ACT,
)


# rtw8821a_table.c:2145 — trans_act_to_cardemu_8821a
ACT_TO_CARDEMU = (
    (0x001F, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   0xFF,                0),
    (0x004F, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 0,              0),
    (0x0049, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 1,              0),
    (0x0006, CUT_ALL, INTF_USB,             ADDR_MAC, CMD_WRITE,   1 << 0,              1 << 0),
    (0x0005, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_WRITE,   1 << 1,              1 << 1),
    (0x0005, CUT_ALL, INTF_ALL,             ADDR_MAC, CMD_POLLING, 1 << 1,              0),
    (0x0000, CUT_ALL, INTF_USB | INTF_SDIO, ADDR_MAC, CMD_WRITE,   1 << 5,              1 << 5),
    (0x0020, CUT_ALL, INTF_USB | INTF_SDIO, ADDR_MAC, CMD_WRITE,   1 << 0,              0),
    (0xFFFF, CUT_ALL, INTF_ALL,             0,        CMD_END,     0,                   0),
)

# rtw8821a_table.c:2193 — trans_cardemu_to_carddis_8821a
CARDEMU_TO_CARDDIS = (
    (0x0007, CUT_ALL, INTF_SDIO,            ADDR_MAC,  CMD_WRITE,   0xFF,                0x20),
    (0x0005, CUT_ALL, INTF_USB | INTF_SDIO, ADDR_MAC,  CMD_WRITE,   (1 << 3) | (1 << 4), 1 << 3),
    (0x0005, CUT_ALL, INTF_PCI,             ADDR_MAC,  CMD_WRITE,   1 << 2,              1 << 2),
    (0x004A, CUT_ALL, INTF_USB,             ADDR_MAC,  CMD_WRITE,   1 << 0,              1),
    (0x0023, CUT_ALL, INTF_SDIO,            ADDR_MAC,  CMD_WRITE,   1 << 4,              1 << 4),
    (0x0086, CUT_ALL, INTF_SDIO,            ADDR_SDIO, CMD_WRITE,   1 << 0,              1 << 0),
    (0x0086, CUT_ALL, INTF_SDIO,            ADDR_SDIO, CMD_POLLING, 1 << 1,              0),
    (0xFFFF, CUT_ALL, INTF_ALL,             0,         CMD_END,     0,                   0),
)

# Top-level flow (rtw8821a_table.c:2247)
CARD_DISABLE_FLOW_8821A: Sequence[Sequence[Sequence[int]]] = (
    ACT_TO_CARDEMU,
    CARDEMU_TO_CARDDIS,
)


def card_enable_flow_8821a(transport: RTL8821AUTransport) -> None:
    """Run the full power-on sequence for the 8821A on a USB host."""
    run_pwr_flow(transport, CARD_ENABLE_FLOW_8821A,
                 intf_mask=INTF_USB, cut_mask=CUT_ALL)


def card_disable_flow_8821a(transport: RTL8821AUTransport) -> None:
    """Run the full power-off sequence for the 8821A on a USB host."""
    run_pwr_flow(transport, CARD_DISABLE_FLOW_8821A,
                 intf_mask=INTF_USB, cut_mask=CUT_ALL)


__all__ = [
    "ADDR_MAC", "ADDR_SDIO",
    "CMD_DELAY", "CMD_END", "CMD_POLLING", "CMD_READ", "CMD_WRITE",
    "CUT_ALL", "DELAY_MS", "DELAY_US",
    "INTF_ALL", "INTF_PCI", "INTF_SDIO", "INTF_USB",
    "CARDDIS_TO_CARDEMU", "CARDEMU_TO_ACT",
    "CARD_ENABLE_FLOW_8821A", "CARD_DISABLE_FLOW_8821A",
    "ACT_TO_CARDEMU", "CARDEMU_TO_CARDDIS",
    "card_enable_flow_8821a", "card_disable_flow_8821a",
    "run_pwr_seq", "run_pwr_flow",
]
