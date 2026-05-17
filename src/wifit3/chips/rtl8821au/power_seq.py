"""Power-on / power-off sequences for RTL8821AU.

Direct translation of the rtw_pwr_seq_cmd tables from
`data_dumps/rtw88-source-v6.18/rtw8821a_table.c`. Each tuple maps 1:1
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
import time
from typing import Iterable, Sequence

from .transport import RTL8821AUTransport

logger = logging.getLogger(__name__)


# Command codes (main.h:929)
CMD_READ = 0x00
CMD_WRITE = 0x01
CMD_POLLING = 0x02
CMD_DELAY = 0x03
CMD_END = 0x04

# Interface masks (main.h:941)
INTF_SDIO = 0x01
INTF_USB = 0x02
INTF_PCI = 0x04
INTF_ALL = 0x0F

# Cut masks (main.h:946)
CUT_ALL = 0xFF

# Address-base (main.h:936)
ADDR_MAC = 0x00
ADDR_SDIO = 0x03

# Delay unit (main.h:957)
DELAY_US = 0
DELAY_MS = 1


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


def _poll(transport: RTL8821AUTransport, addr: int, mask: int, target: int,
          attempts: int = 1000, interval_s: float = 0.001) -> bool:
    """Poll `addr` byte until `(read & mask) == (target & mask)`.

    Defaults give a ~1s budget which is far more generous than the kernel's
    udelay loop, because every read here is a USB control transfer (~1ms).
    """
    desired = target & mask
    for _ in range(attempts):
        v = transport.read8(addr)
        if (v & mask) == desired:
            return True
        time.sleep(interval_s)
    return False


def run_pwr_seq(transport: RTL8821AUTransport,
                seq: Iterable[Sequence[int]],
                intf_mask: int = INTF_USB,
                cut_mask: int = CUT_ALL) -> None:
    """Execute one rtw_pwr_seq_cmd table (one sub-sequence).

    Skips entries that don't match `intf_mask`. Raises IOError on a failed
    POLLING step (matches the kernel's `-EBUSY` semantics).
    """
    for offset, cm, im, base, cmd, mask, value in seq:
        if cmd == CMD_END:
            return
        if not (im & intf_mask) or not (cm & cut_mask):
            continue
        if base == ADDR_SDIO:
            # We never go through SDIO from USB, but the parser handles it
            # by ORing in SDIO_LOCAL_OFFSET. We'd never hit this in practice.
            logger.debug("skipping SDIO addr 0x%04x", offset)
            continue

        if cmd == CMD_WRITE:
            cur = transport.read8(offset)
            new = (cur & ~mask) | (value & mask)
            new &= 0xFF
            logger.debug(
                "pwr WRITE  addr=0x%04x  mask=0x%02x  val=0x%02x  (read 0x%02x -> write 0x%02x)",
                offset, mask, value, cur, new,
            )
            transport.write8(offset, new)
        elif cmd == CMD_POLLING:
            logger.debug("pwr POLL   addr=0x%04x  mask=0x%02x  target=0x%02x",
                         offset, mask, value)
            if not _poll(transport, offset, mask, value):
                raise IOError(
                    f"power-seq poll failed: addr=0x{offset:04x} "
                    f"mask=0x{mask:02x} target=0x{value:02x}"
                )
        elif cmd == CMD_DELAY:
            # `value` selects unit; `offset` is the count (per kernel parser)
            count = offset
            if value == DELAY_US:
                time.sleep(count / 1_000_000)
            else:
                time.sleep(count / 1_000)
        elif cmd == CMD_READ:
            transport.read8(offset)
        else:
            raise ValueError(f"unknown pwr_seq cmd {cmd}")


def card_enable_flow_8821a(transport: RTL8821AUTransport) -> None:
    """Run the full power-on sequence for the 8821A on a USB host."""
    for sub in CARD_ENABLE_FLOW_8821A:
        run_pwr_seq(transport, sub, intf_mask=INTF_USB, cut_mask=CUT_ALL)
