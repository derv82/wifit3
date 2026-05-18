"""Generic `rtw_pwr_seq_cmd` runtime for the rtw88 family.

Semantics (mirroring `rtw_sub_pwr_seq_parser` in `mac.c:185`):
- WRITE   : value = (read8(addr) & ~mask) | (cmd_value & mask); write8(addr, value)
- POLLING : poll until (read8(addr) & mask) == (cmd_value & mask)
- DELAY   : if cmd_value == DELAY_US: usleep(addr) else: msleep(addr)
- END     : terminate sub-sequence

Each entry tuple matches the C struct `rtw_pwr_seq_cmd` field order exactly:
(offset, cut_mask, intf_mask, base, cmd, mask, value).

Per-chip sequence *tables* live in each chip's `chips.<chip>.power_seq`.
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Sequence

from .transport import Rtw88Transport

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


def _poll(transport: Rtw88Transport, addr: int, mask: int, target: int,
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


def run_pwr_seq(transport: Rtw88Transport,
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
                "pwr WRITE  addr=0x%04x  mask=0x%02x  val=0x%02x  "
                "(read 0x%02x -> write 0x%02x)",
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
            count = offset
            if value == DELAY_US:
                time.sleep(count / 1_000_000)
            else:
                time.sleep(count / 1_000)
        elif cmd == CMD_READ:
            transport.read8(offset)
        else:
            raise ValueError(f"unknown pwr_seq cmd {cmd}")


def run_pwr_flow(transport: Rtw88Transport,
                 flow: Iterable[Iterable[Sequence[int]]],
                 *,
                 intf_mask: int = INTF_USB,
                 cut_mask: int = CUT_ALL) -> None:
    """Run an ordered list of sub-sequences (a "flow")."""
    for sub in flow:
        run_pwr_seq(transport, sub, intf_mask=intf_mask, cut_mask=cut_mask)
